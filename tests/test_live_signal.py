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


def test_size_positions_rotation_never_pushes_a_name_far_over_target():
    """补手只能让持仓更接近等权目标，不能把买不起的票的钱堆到便宜票上。

    实测 11 万元 × 40% 分 100 只：每只预算 440 元，绝大多数一手都买不起，
    轮转把余量全加给了 4 只便宜票，单票权重 36%——等权组合变成了集中持仓。
    """
    px = {"CHEAP": 2.0, **{f"E{i}": 50.0 for i in range(9)}}
    sh = size_positions(list(px), px, 10_000.0)     # 每只预算 1,000
    assert sh["CHEAP"] * px["CHEAP"] <= 2_000, sh   # 原来是 5,000 股 = 全部预算
    assert all(sh[f"E{i}"] == 0 for i in range(9))


def test_plan_reports_unfunded_names_and_min_equity():
    """一手超过单只预算 2 倍的票买不进去：必须点名，并给出买齐所需的总资产。"""
    prices = {"A": 10.0, "B": 10.0, "C": 500.0}
    plan = build_plan("2026-10-20", prices, Sample(ranked=["A", "B", "C"], n=3),
                      ["A", "B", "C"], capital=30_000, position=1.0, cash_buffer=0.0)
    assert plan.unfunded == ["C"]
    # 每只预算要 ≥ 一手的一半：3 × 50,000 / 2 = 75,000
    assert plan.min_equity == pytest.approx(75_000.0)
    assert any("资金不足" in w and "75,000" in w for w in plan.warnings)
    assert plan.to_json()["unfunded"] == ["C"]


def test_plan_counts_names_below_min_trade_value_as_unfunded():
    """每只预算 1,500 < 最小下单额 2,000：一手买得起，但买单会被过滤掉。

    不算进资金不足的话，结果是一笔不买、屏幕上却写着"无需调仓"。
    """
    prices = {"A": 5.0, "B": 5.0}
    plan = build_plan("2026-10-20", prices, Sample(ranked=["A", "B"], n=2),
                      ["A", "B"], capital=3_000, position=1.0, cash_buffer=0.0)
    assert plan.orders.empty
    assert plan.unfunded == ["A", "B"]
    assert plan.min_equity == pytest.approx(4_000.0)   # 2 只 × 2,000


def test_plan_without_unfunded_names_has_no_capital_warning():
    prices = {"A": 10.0, "B": 10.0}
    plan = build_plan("2026-10-20", prices, Sample(ranked=["A", "B"], n=2),
                      ["A", "B"], capital=100_000, position=1.0, cash_buffer=0.0)
    assert plan.unfunded == []
    assert not any("资金不足" in w for w in plan.warnings)


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


def test_live_signal_merges_status_and_blocks_limit_down():
    """实盘信号脚本必须：
    ① 合并 status 表的 is_suspended（bars.is_suspended 恒为 False）；
    ② 将跌停持仓（close <= limit_down + 1e-4）移出 tradeable（冻结，算仓位不挂卖单），
       但**保留在 investable 中**（跌停股卖难买易，可买不可卖，对齐回测 execution.py 口径 — A1）；
    ③ 涨停股**不再**从 investable 剔除：信号层只出意图，追不追由下单时的执行日闸门判
       （`gm_bridge.place-orders`，与 backtest/execution.py 同口径）。旧口径用 t 日收盘
       近似 t+1 开盘，对"专买涨停股"的策略等于永远买不进（hype_watch 实测 3/3 被拦）；
    ④ 当 status 非空但缺少 as_of 当日记录（停牌数据落后）时必须显式告警（A3）。
    """
    from pathlib import Path
    import importlib.util

    p = Path(__file__).resolve().parents[1] / "scripts" / "live_signal.py"
    spec = importlib.util.spec_from_file_location("live_signal_script", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert hasattr(mod, "_compute_tradeability"), "live_signal.py 应提供 _compute_tradeability 辅助函数"
    as_of = pd.Timestamp("2026-10-20")
    bars = pd.DataFrame([
        # NORMAL: 正常可买卖
        {"trade_date": as_of, "symbol": "000001", "close": 10.0, "limit_up": 11.0, "limit_down": 9.0, "limit_exempt": False, "is_suspended": False},
        # SUSP_IN_STATUS: bars 里 is_suspended=False，但 status 表里 is_suspended=True
        {"trade_date": as_of, "symbol": "000002", "close": 10.0, "limit_up": 11.0, "limit_down": 9.0, "limit_exempt": False, "is_suspended": False},
        # LIMIT_DOWN: 收盘跌停，持仓卖不出（不在 tradeable），但新开仓允许买入（在 investable）
        {"trade_date": as_of, "symbol": "000003", "close": 9.0, "limit_up": 11.0, "limit_down": 9.0, "limit_exempt": False, "is_suspended": False},
        # LIMIT_UP: 收盘涨停，已持仓可卖（在 tradeable），但新开仓不选（不在 investable）
        {"trade_date": as_of, "symbol": "000004", "close": 11.0, "limit_up": 11.0, "limit_down": 9.0, "limit_exempt": False, "is_suspended": False},
    ])
    status = pd.DataFrame([
        {"trade_date": as_of, "symbol": "000001", "is_st": False, "is_suspended": False},
        {"trade_date": as_of, "symbol": "000002", "is_st": False, "is_suspended": True},
        {"trade_date": as_of, "symbol": "000003", "is_st": False, "is_suspended": False},
        {"trade_date": as_of, "symbol": "000004", "is_st": False, "is_suspended": False},
    ])
    universe = ["000001", "000002", "000003", "000004"]
    tradeable, investable = mod._compute_tradeability(bars, status, universe, as_of, held=[])
    assert "000001" in tradeable and "000001" in investable
    assert "000002" not in tradeable and "000002" not in investable, "status 停牌必须不可买不可卖"
    assert "000003" in tradeable and "000003" in investable, "未持有的跌停股可买，须能分到股数"
    assert "000004" in tradeable and "000004" in investable, \
        "未持有的涨停股照常入选（买入意图照出，闸门在下单时拦）"

    tradeable, investable = mod._compute_tradeability(bars, status, universe, as_of,
                                                      held=["000003", "000004"])
    assert "000003" not in tradeable, "已持有的跌停股冻结（不可卖）"
    assert "000004" in tradeable and "000004" in investable, "已持有的涨停股留在样本、可卖"


def test_live_signal_warns_when_status_lags_behind_as_of(capsys):
    """A3：status 非空但不含 as_of（停牌数据落后于行情）时，必须显式告警，不能静默放行。"""
    from pathlib import Path
    import importlib.util

    p = Path(__file__).resolve().parents[1] / "scripts" / "live_signal.py"
    spec = importlib.util.spec_from_file_location("live_signal_script", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    as_of = pd.Timestamp("2026-10-20")
    prev_day = pd.Timestamp("2026-10-19")
    bars = pd.DataFrame([
        {"trade_date": as_of, "symbol": "000001", "close": 10.0, "limit_up": 11.0, "limit_down": 9.0, "limit_exempt": False, "is_suspended": False},
    ])
    # status 只到前一天 2026-10-19，缺少 as_of 2026-10-20
    status_lag = pd.DataFrame([
        {"trade_date": prev_day, "symbol": "000001", "is_st": False, "is_suspended": False},
    ])
    mod._compute_tradeability(bars, status_lag, ["000001"], as_of)
    out = capsys.readouterr().out
    assert "停牌数据落后" in out and "2026-10-19" in out, f"status 少一天必须打印停牌数据落后告警，实际输出: {out!r}"


def _load_live_signal_script():
    from pathlib import Path
    import importlib.util

    p = Path(__file__).resolve().parents[1] / "scripts" / "live_signal.py"
    spec = importlib.util.spec_from_file_location("live_signal_script", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _limit_scenario():
    """A 正常、B 收盘跌停、C 收盘涨停、D/E 正常；样本按 A..E 排名，n=3。"""
    as_of = pd.Timestamp("2026-10-20")
    closes = {"A": 10.0, "B": 9.0, "C": 11.0, "D": 10.0, "E": 10.0}
    bars = pd.DataFrame([
        {"trade_date": as_of, "symbol": s, "close": c, "limit_up": 11.0, "limit_down": 9.0,
         "limit_exempt": False, "is_suspended": False} for s, c in closes.items()])
    status = pd.DataFrame([
        {"trade_date": as_of, "symbol": s, "is_st": False, "is_suspended": False} for s in closes])
    return as_of, bars, status, list(closes), {s: 10.0 for s in closes}


def test_unheld_limit_down_pick_gets_bought_end_to_end():
    """未持有的跌停股进了样本，就必须分到目标股数（跌停可买）。

    回归：曾经把所有跌停股移出 tradeable，build_plan 只给 tradeable 定量，
    结果它占着样本名额却是 0 股，1/n 资金闲置且不顺位补人。

    2026-10-27 起涨停股 C 也照常入选（买入意图照出，下单时由执行日闸门拦）：
    样本就是排名前 3 的 A/B/C，不再因 C 涨停而顺位补 D。
    """
    mod = _load_live_signal_script()
    as_of, bars, status, uni, prices = _limit_scenario()
    tradeable, investable = mod._compute_tradeability(bars, status, uni, as_of, held=[])
    plan = build_plan(as_of, prices, Sample(ranked=uni, n=3), investable,
                      current_holdings={}, capital=1_000_000, position=1.0,
                      cash_buffer=0.0, tradeable=tradeable)
    buys = dict(zip(plan.orders["symbol"], plan.orders["shares"]))
    assert set(buys) == {"A", "B", "C"}, f"跌停的 B 应被买入，实际订单 {buys}"
    assert all(v > 0 for v in buys.values())


def test_held_limit_up_sample_stock_not_cleared_end_to_end():
    """已持有且仍在样本内的股票当日涨停，不能被当成"不可投资"踢出样本而清仓。"""
    mod = _load_live_signal_script()
    as_of, bars, status, uni, prices = _limit_scenario()
    hold = {"A": 33300, "C": 33300, "D": 33300}
    tradeable, investable = mod._compute_tradeability(bars, status, uni, as_of, held=hold)
    plan = build_plan(as_of, prices, Sample(ranked=uni, n=3), investable,
                      current_holdings=hold, capital=1_000_000, position=1.0,
                      cash_buffer=0.0, tradeable=tradeable)
    clears = plan.orders.loc[plan.orders["note"] == "clear", "symbol"].tolist()
    assert "C" not in clears, f"涨停的持仓 C 被清仓：{plan.orders.values.tolist()}"
    # B 跌停但未持有、排名第 2，可买 → 前 3 名是 A/B/C
    assert set(plan.target.loc[plan.target["target_shares"] > 0, "symbol"]) == {"A", "B", "C"}


def test_held_limit_down_still_frozen_end_to_end():
    """已持有的跌停股仍须冻结：不卖、按原股数占仓位。"""
    mod = _load_live_signal_script()
    as_of, bars, status, uni, prices = _limit_scenario()
    hold = {"B": 20000}
    tradeable, investable = mod._compute_tradeability(bars, status, ["A", "C", "D", "E"], as_of,
                                                      held=hold)
    assert "B" not in tradeable
    plan = build_plan(as_of, prices, Sample(ranked=["A", "C", "D", "E"], n=3), investable,
                      current_holdings=hold, capital=1_000_000, position=1.0,
                      cash_buffer=0.0, tradeable=tradeable)
    b = plan.orders[plan.orders["symbol"] == "B"]
    assert b.empty or (b["side"] == "hold").all(), f"跌停持仓 B 不应有卖单：{b.values.tolist()}"


def test_status_missing_in_window_warns(capsys):
    """status 在回看窗口内完全为空/读取失败（落后超过窗口）时也必须告警，不能静默放行。"""
    mod = _load_live_signal_script()
    as_of, bars, _, uni, _ = _limit_scenario()
    for st in (None, pd.DataFrame(columns=["trade_date", "symbol", "is_suspended"])):
        mod._compute_tradeability(bars, st, uni, as_of, held=[])
        out = capsys.readouterr().out
        assert "停牌数据" in out and "[warn]" in out, f"status={st!r} 时应告警，实际 {out!r}"


