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
