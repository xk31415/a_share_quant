"""实盘信号层测试。

重点不在"能不能跑"，而在几条会直接导致亏钱的规则：
整手约束、清仓可卖零股、冻结持仓不下单、样本固定不漂移。
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from a_share_quant.live.signal import (
    Sample,
    build_plan,
    diff_orders,
    quote_symbols,
    rank_universe,
    size_positions,
)


# ---------------------------------------------------------------- 定量
def test_size_positions_respects_lot_and_spends_cash():
    """整手约束 + 余量轮转补手：不能留一大笔钱在账上。"""
    syms = ["A", "B", "C"]
    px = {"A": 10.0, "B": 13.0, "C": 27.0}
    sh = size_positions(syms, px, 100_000.0, lot_size=100)
    assert all(v % 100 == 0 for v in sh.values())
    spent = sum(sh[s] * px[s] for s in syms)
    # 每只预算 33,333，整手下取后余量必须被补回去
    assert spent > 98_000, f"只花了 {spent:.0f}/100000，余量没补回去"
    assert spent <= 100_000 + 1e-9


def test_size_positions_each_name_roughly_equal():
    px = {"A": 10.0, "B": 10.0, "C": 10.0}
    sh = size_positions(["A", "B", "C"], px, 30_000.0)
    assert sh == {"A": 1000, "B": 1000, "C": 1000}


def test_size_positions_raises_on_missing_price():
    """缺价格必须报错：静默跳过会让这只票没钱买，权重凭空消失。"""
    with pytest.raises(ValueError, match="缺少有效价格"):
        size_positions(["A", "B"], {"A": 10.0}, 10_000.0)


def test_size_positions_zero_budget_is_all_zero():
    assert size_positions(["A"], {"A": 10.0}, 0.0) == {"A": 0}


# ---------------------------------------------------------------- 差分
def test_diff_orders_buys_in_lots():
    orders = diff_orders({"A": 1050}, {}, {"A": 10.0})
    assert len(orders) == 1
    assert orders[0]["side"] == "buy"
    assert orders[0]["shares"] == 1000, "买入必须整手（1050 -> 1000）"


def test_diff_orders_sells_in_lots_but_allows_odd_lot_on_clearance():
    """A 股卖出也受整手约束，只有清仓可以带零股（上证函〔2014〕301 号）。"""
    partial = diff_orders({"A": 800}, {"A": 1050}, {"A": 10.0})
    assert partial[0]["shares"] == 200, "减仓 250 股只能卖 200 股（整手）"

    clear = diff_orders({"A": 0}, {"A": 1050}, {"A": 10.0})
    assert clear[0]["side"] == "sell"
    assert clear[0]["shares"] == 1050, "清仓必须能卖掉零股"
    assert clear[0]["note"] == "clear"


def test_diff_orders_skips_small_trades():
    """1 手 10 元股只有 1000 元，低于阈值不该下单（佣金划不来）。"""
    assert diff_orders({"A": 100}, {}, {"A": 10.0}, min_trade_value=2000) == []
    # 清仓不受阈值限制：留着 100 股没有任何意义
    assert diff_orders({}, {"A": 100}, {"A": 10.0}, min_trade_value=2000)[0]["shares"] == 100


def test_diff_orders_flags_missing_price():
    orders = diff_orders({"A": 0}, {"A": 500}, {})
    assert orders[0]["side"] == "hold"
    assert orders[0]["note"] == "blocked:no_price"


# ---------------------------------------------------------------- 样本
def test_rank_universe_matches_equal_weight_hash():
    """seed=0 必须与 strategies.equal_weight 同一口径，否则实盘样本
    和回测样本对不上，历史结果无法复用。"""
    univ = [f"{i:06d}" for i in range(1, 200)]
    expected = sorted(univ, key=lambda s: hashlib.sha1(s.encode()).hexdigest())
    assert rank_universe(univ, 0) == expected


def test_sample_backfills_by_rank_when_symbol_not_investable():
    sample = Sample(ranked=["A", "B", "C", "D", "E"], n=3)
    picked, skipped = sample.active(investable=["A", "C", "D", "E"])
    assert picked == ["A", "C", "D"], "B 不可投资时按名次由 D 补位"
    assert skipped == ["B"]


def test_sample_roundtrip(tmp_path):
    sample = Sample(ranked=["A", "B", "C"], n=2, seed=3, data_dir="d",
                    as_of="2026-10-20", note="冻结")
    p = sample.save(tmp_path / "s.json")
    back = Sample.load(p)
    assert back.ranked == sample.ranked and back.n == 2 and back.seed == 3
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["note"] == "冻结" and raw["as_of"] == "2026-10-20"


def test_sample_rejects_short_pool():
    with pytest.raises(ValueError, match="不足"):
        Sample(ranked=["A", "B"], n=5)


# ---------------------------------------------------------------- 计划
def _sample(n=3):
    return Sample(ranked=["A", "B", "C", "X", "Y"], n=n)


def test_plan_position_scales_budget():
    prices = {"A": 10.0, "B": 10.0, "C": 10.0}
    full = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                      capital=1_000_000, position=1.0, cash_buffer=0.0)
    half = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                      capital=1_000_000, position=0.5, cash_buffer=0.0)
    assert full.target["target_shares"].sum() == 100_000
    assert half.target["target_shares"].sum() == 50_000


def test_plan_freezes_untradeable_holding_and_charges_its_value():
    """停牌股：不下单、但要占仓位——否则会拿它的市值再买一遍别的票。"""
    prices = {"A": 10.0, "B": 10.0, "C": 10.0}
    plan = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                      current_holdings={"A": 10_000, "B": 0}, capital=1_000_000,
                      position=1.0, cash_buffer=0.0,
                      tradeable={"B", "C"})       # A 停牌
    a_orders = plan.orders[plan.orders["symbol"] == "A"]
    assert a_orders.empty, f"A 停牌不该有委托，实际 {a_orders.to_dict('records')}"
    a_row = plan.target[plan.target["symbol"] == "A"].iloc[0]
    assert a_row["target_shares"] == 10_000 and not a_row["tradeable"]
    # 预算扣除 A 的 10 万后只剩 90 万给 B、C
    bc = plan.target[plan.target["symbol"].isin(["B", "C"])]
    assert bc["target_value"].sum() == pytest.approx(900_000, rel=1e-6)


def test_plan_clears_holdings_outside_sample():
    prices = {"A": 10.0, "B": 10.0, "C": 10.0, "Z": 20.0}
    plan = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                      current_holdings={"Z": 3000}, capital=1_000_000,
                      position=1.0, cash_buffer=0.0,
                      tradeable={"A", "B", "C", "Z"})
    z = plan.orders[plan.orders["symbol"] == "Z"]
    assert len(z) == 1 and z.iloc[0]["side"] == "sell" and z.iloc[0]["shares"] == 3000
    assert any("不在当前样本内" in w for w in plan.warnings)


def test_plan_warns_when_capital_below_holdings_value():
    """--capital 传成"可用现金"是常见误用，必须显式告警。"""
    prices = {"A": 10.0, "B": 10.0, "C": 10.0}
    plan = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                      current_holdings={"A": 5000}, capital=10_000,
                      position=1.0, cash_buffer=0.0)
    assert any("现金为负" in w for w in plan.warnings), plan.warnings


def test_plan_raises_when_holding_has_no_price():
    """持仓缺价格无法估值总资产——必须报错，不能按 0 算。"""
    with pytest.raises(ValueError, match="无法估值"):
        build_plan("2026-10-20", {"A": 10.0}, _sample(), ["A", "B", "C"],
                   current_holdings={"ZZZ": 100}, capital=1_000_000)


def test_plan_reports_shortfall_in_sample():
    prices = {"A": 10.0, "B": 10.0}
    plan = build_plan("2026-10-20", prices, _sample(3), ["A", "B"],
                      capital=1_000_000, position=1.0)
    assert len(plan.target) == 2
    assert any("少于目标" in w for w in plan.warnings)


def test_plan_rejects_bad_position():
    with pytest.raises(ValueError):
        build_plan("2026-10-20", {"A": 1.0}, _sample(), ["A"], position=0)


def test_plan_is_idempotent_when_portfolio_matches_target():
    """把目标持仓回灌再跑一次，必须零订单。

    实盘最容易犯的错是"每次跑都换一批单"，白交佣金还偏离目标。
    这条也顺带锁住"目标股数不随调用次数漂移"。
    """
    prices = {"A": 10.0, "B": 13.0, "C": 27.0}
    first = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                       capital=1_000_000, position=0.40, cash_buffer=0.01)
    assert len(first.orders) == 3
    hold = dict(zip(first.target["symbol"], first.target["target_shares"]))
    second = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                        current_holdings=hold, capital=1_000_000,
                        position=0.40, cash_buffer=0.01)
    assert second.orders.empty, second.orders.to_dict("records")


def test_plan_never_exceeds_budget():
    prices = {"A": 7.77, "B": 13.13, "C": 27.27, "D": 3.03}
    plan = build_plan("2026-10-20", prices,
                      Sample(ranked=["A", "B", "C", "D"], n=4), ["A", "B", "C", "D"],
                      capital=1_000_000, position=0.40, cash_buffer=0.01)
    assert plan.target["target_value"].sum() <= plan.investable + 1e-6


def test_frozen_holding_is_not_warned_as_clearing():
    """停牌持仓只能报"保持原股数"，不能同时报"目标为 0（清仓）"（R05 F-20）。

    两条提示相反：一条说清仓、一条说冻结，而订单里根本没有它的卖单。
    照"清仓"那句手工下单，会把一只仍在冻结样本里的股票卖掉。
    """
    prices = {"A": 10.0, "B": 10.0, "C": 10.0}
    plan = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                      current_holdings={"A": 10_000}, capital=1_000_000,
                      position=1.0, cash_buffer=0.0,
                      tradeable={"B", "C"})       # A 停牌
    clearing = [w for w in plan.warnings if "清仓" in w]
    frozen = [w for w in plan.warnings if "保持原股数" in w]
    assert frozen, plan.warnings
    assert not clearing, f"停牌持仓不该出现清仓提示：{clearing}"


def test_really_stale_holding_still_warned_as_clearing():
    """真·剔除样本且可交易的持仓，仍然要报清仓，别把 F-20 修过头。"""
    prices = {"A": 10.0, "B": 10.0, "C": 10.0, "Z": 20.0}
    plan = build_plan("2026-10-20", prices, _sample(), ["A", "B", "C"],
                      current_holdings={"Z": 3000}, capital=1_000_000,
                      position=1.0, cash_buffer=0.0,
                      tradeable={"A", "B", "C", "Z"})
    clearing = [w for w in plan.warnings if "清仓" in w]
    assert clearing and "Z" in clearing[0], plan.warnings


def test_quote_symbols_covers_full_ranked():
    """取价必须覆盖完整 ranked，否则补位越过窗口就抛错、当天不出单（R05 F-21）。"""
    ranked = [f"{i:06d}" for i in range(400)]
    sample = Sample(created="x", data_dir="d", as_of="2026-10-20", n=100,
                    seed=0, ranked=ranked)
    got = quote_symbols(sample, {"A": 100})
    assert len(got) == 401
    assert ranked[-1] in got and "A" in got


# ---------------------------------------------------------------- R06 F-30
def test_non_quarter_month_really_runs_the_check():
    """R06 F-30：非季度月打印"只检查不下单"，但检查原来根本没执行。

    熔断 2 的 return 在 build_plan 之前，目标权重、订单、告警一行都出不来。
    这里守住一条结构性事实：熔断之后到 build_plan 之间不能再有 return。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "live_signal.py"
           ).read_text(encoding="utf-8")
    i_guard = src.index("check_only = bool(")
    i_plan = src.index("plan = build_plan(")
    assert i_guard < i_plan, "熔断判断必须在 build_plan 之前（设标志，不是 return）"
    assert "return" not in src[i_guard:i_plan], \
        "熔断与 build_plan 之间不能有 return，否则检查又是空的"
    # 非季度月必须在"渲染完检查报告之后、写文件之前"结束
    # （rindex：熔断打印处也有一个 if check_only:，在 build_plan 之前）
    assert src.rindex("if check_only:") > i_plan
    assert src.index("write_plan(") > src.rindex("if check_only:")


# ---------------------------------------------------------------- R08 F-43
def test_quarter_month_check_uses_the_requested_month_not_the_rolled_back_day():
    """R08 F-43：季度调仓月的判断要用**请求日**的月份。

    非交易日的 `--as-of` 会被回滚到上一交易日，于是 2023-04-01（季度月）
    变成 2023-03-31（非季度月），该调仓的一天被当成"只检查"跳过；
    2025-11-01 则反过来，非季度月被当成季度月下单。

    判断逻辑嵌在 `main()` 里（需要数据目录才能跑），所以这里守住两条结构事实：
    ① 存在 `quarter_month` 且取自 `args.as_of`；② `check_only` 用的是它。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "live_signal.py"
           ).read_text(encoding="utf-8")

    i = src.index("quarter_month = (")
    seg = src[i:i + 300]
    assert "args.as_of" in seg, "quarter_month 必须取自请求日"

    j = src.index("check_only = bool(")
    assert "quarter_month not in DEFAULT_REBALANCE_MONTHS" in src[j:j + 250], \
        "check_only 必须用 quarter_month，不能用回滚后的 as_of.month"

    # 回滚本身要保留：数据对账确实要用交易日
    assert "_resolve_as_of(cal, args.as_of)" in src


# ---------------------------------------------------------------- R08 F-43
def test_quarter_month_check_uses_the_requested_month_not_the_rolled_back_day():
    """R08 F-43：季度调仓月的判断要用**请求日**的月份。

    非交易日的 `--as-of` 会被回滚到上一交易日，于是 2023-04-01（季度月）
    变成 2023-03-31（非季度月），该调仓的一天被当成"只检查"跳过；
    2025-11-01 则反过来，非季度月被当成季度月下单。

    判断逻辑嵌在 `main()` 里（需要数据目录才能跑），所以这里守住两条结构事实：
    ① 存在 `quarter_month` 且取自 `args.as_of`；② `check_only` 用的是它。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "live_signal.py"
           ).read_text(encoding="utf-8")

    i = src.index("quarter_month = (")
    seg = src[i:i + 300]
    assert "args.as_of" in seg, "quarter_month 必须取自请求日"

    j = src.index("check_only = bool(")
    assert "quarter_month not in DEFAULT_REBALANCE_MONTHS" in src[j:j + 250], \
        "check_only 必须用 quarter_month，不能用回滚后的 as_of.month"

    # 回滚本身要保留：数据对账确实要用交易日
    assert "_resolve_as_of(cal, args.as_of)" in src
