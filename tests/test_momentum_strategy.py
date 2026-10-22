"""动量策略：选股正确性与防未来函数。"""
import numpy as np
import pandas as pd

from a_share_quant.strategies import TopNMomentum


def _bars(n=80, last_spike="C"):
    dates = pd.bdate_range("2024-01-01", periods=n)
    rows = []
    for sym in ("A", "B", "C", "D"):
        for i, d in enumerate(dates):
            base = 10.0
            if sym == "A":
                px = base * (1.0 + 0.004 * i)          # 稳步上涨
            elif sym == "B":
                px = base * np.ones(n)[i]              # 横盘
            elif sym == "C":
                px = base * (1.0 + (0.5 if i >= n - 3 else 0.0))   # 最后三天暴涨
            else:
                px = base * (1.0 - 0.002 * i)          # 阴跌
            rows.append({"trade_date": d, "symbol": sym, "close": px,
                         "adj_close": px, "amount": px * 1e6})
    return pd.DataFrame(rows), dates


def test_picks_strongest_momentum_single_stock():
    bars, dates = _bars()
    strat = TopNMomentum(top_n=1, window=60, skip=5)
    t = strat.generate_target(dates[-1], bars, ["A", "B", "C", "D"])
    assert list(t.weights) == ["A"], f"应选 A，实际 {list(t.weights)}"
    assert abs(sum(t.weights.values()) - 1.0) < 1e-12


def test_recent_spike_is_skipped():
    """最近 5 天暴涨不该被算进动量（12-1 动量的 skip 段）。"""
    bars, dates = _bars()
    # 不跳过时 C 会入选；跳过 5 天后 C 的暴涨被排除
    with_skip = TopNMomentum(top_n=1, window=60, skip=5)
    no_skip = TopNMomentum(top_n=1, window=60, skip=0)
    assert list(with_skip.generate_target(dates[-1], bars, ["A", "B", "C"]).weights) == ["A"]
    assert list(no_skip.generate_target(dates[-1], bars, ["A", "B", "C"]).weights) == ["C"]


def test_only_uses_data_up_to_as_of():
    """把未来数据整段删掉，信号必须完全一致（防未来函数）。"""
    bars, dates = _bars()
    strat = TopNMomentum(top_n=2, window=60, skip=5)
    full = strat.generate_target(dates[-1], bars, ["A", "B", "C", "D"])
    truncated = bars[bars["trade_date"] <= dates[-1]]
    again = strat.generate_target(dates[-1], truncated, ["A", "B", "C", "D"])
    assert dict(full.weights) == dict(again.weights)


def test_insufficient_history_returns_empty():
    bars, dates = _bars(n=20)
    strat = TopNMomentum(top_n=2, window=60, skip=5)
    assert strat.generate_target(dates[-1], bars, ["A", "B"]).weights == {}


def test_universe_filter_is_respected():
    bars, dates = _bars()
    strat = TopNMomentum(top_n=2, window=60, skip=5)
    t = strat.generate_target(dates[-1], bars, ["B", "D"])   # 最强的 A 不在池子里
    assert set(t.weights) <= {"B", "D"}
    assert len(t.weights) == 2


# ---------------------------------------------------------------- 行业中性（R08 后续）
def _industry_bars(n=80):
    """三个行业各两只，行业间动量明显分层：

    行业内排序都是  1 > 2；行业间  IND1 > IND2 > IND3。
    于是「非中性取前 2」会全落在 IND1，而「行业中性 k=1」必然每行业一只。
    """
    dates = pd.bdate_range("2024-01-01", periods=n)
    growth = {"A1": 0.0070, "A2": 0.0060,
              "B1": 0.0050, "B2": 0.0040,
              "C1": 0.0030, "C2": 0.0020}
    rows = []
    for sym, g in growth.items():
        for i, d in enumerate(dates):
            px = 10.0 * (1.0 + g) ** i
            rows.append({"trade_date": d, "symbol": sym, "close": px,
                         "adj_close": px, "amount": px * 1e6})
    ind = {"A1": "IND1", "A2": "IND1", "B1": "IND2",
           "B2": "IND2", "C1": "IND3", "C2": "IND3"}
    return pd.DataFrame(rows), dates, ind


def test_without_industry_map_the_default_output_is_unchanged():
    """不传 industry_map 时必须走原路径，选股结果与以前逐位一致。

    这个项目的硬约定：给参数加默认值时，默认分支的输出必须不变
    （曾经给等权加 seed 时把键改了，年化 9.15% → 4.55%）。
    """
    bars, dates, _ = _industry_bars()
    plain = TopNMomentum(top_n=2, window=60, skip=5)
    explicit_none = TopNMomentum(top_n=2, window=60, skip=5, industry_map=None)
    a = plain.generate_target(dates[-1], bars, ["A1", "A2", "B1", "B2", "C1", "C2"])
    b = explicit_none.generate_target(dates[-1], bars, ["A1", "A2", "B1", "B2", "C1", "C2"])
    assert a.weights == b.weights
    assert list(a.weights) == ["A1", "A2"], f"非中性取全场前 2，实际 {list(a.weights)}"


def test_industry_neutral_picks_one_per_industry_from_each():
    """行业中性 k=1：必须每个行业各取一只，而不是全落在最强的那个行业。"""
    bars, dates, ind = _industry_bars()
    strat = TopNMomentum(top_n=2, window=60, skip=5, industry_map=ind)
    t = strat.generate_target(dates[-1], bars, list(ind))
    picked = sorted(t.weights)
    assert picked == ["A1", "B1", "C1"], f"应每行业最强的，实际 {picked}"
    assert abs(sum(t.weights.values()) - 1.0) < 1e-12
    # 等权：3 只各 1/3
    assert all(abs(w - 1 / 3) < 1e-12 for w in t.weights.values())


def test_industry_neutral_respects_reverse_direction():
    """反转 + 行业中性：每个行业内部取动量**最弱**的那只。"""
    bars, dates, ind = _industry_bars()
    strat = TopNMomentum(top_n=2, window=60, skip=5, reverse=True,
                         industry_map=ind)
    t = strat.generate_target(dates[-1], bars, list(ind))
    assert sorted(t.weights) == ["A2", "B2", "C2"], \
        f"应每行业最弱的，实际 {sorted(t.weights)}"


def test_industry_neutral_k_per_industry_takes_k_from_each():
    bars, dates, ind = _industry_bars()
    strat = TopNMomentum(top_n=2, window=60, skip=5, industry_map=ind,
                         k_per_industry=2)
    t = strat.generate_target(dates[-1], bars, list(ind))
    assert len(t.weights) == 6, f"3 行业 × 2 只 = 6，实际 {len(t.weights)}"


def test_single_stock_industry_is_skipped():
    """只有一个成员的行业没有"组内选择"，不参与（与低波的实现一致）。"""
    bars, dates, ind = _industry_bars()
    ind2 = dict(ind)
    ind2["D1"] = "IND4"          # 行业里只有 D1 一只
    bars = pd.concat([bars, bars[bars["symbol"] == "A1"].assign(symbol="D1")])
    strat = TopNMomentum(top_n=2, window=60, skip=5, industry_map=ind2)
    t = strat.generate_target(dates[-1], bars, list(ind2))
    assert "D1" not in t.weights, "单成员行业不该入选"
