"""低波动策略：选股正确性、流动性过滤、防未来函数。"""
import numpy as np
import pandas as pd

from a_share_quant.strategies import LowVolatility


def _bars(n=80):
    """构造 4 只标的：STABLE 波动最小，WILD 最大；ILLIQ 成交额极小。"""
    dates = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(7)
    spec = {"STABLE": 0.001, "MID": 0.01, "WILD": 0.04, "ILLIQ": 0.002}
    rows = []
    for sym, sigma in spec.items():
        px = 10.0 * np.cumprod(1.0 + rng.normal(0.0, sigma, n))
        amt = np.full(n, 1e8)
        if sym == "ILLIQ":
            amt = np.full(n, 1e4)        # 成交额极小，流动性过滤时应被剔除
        for i, d in enumerate(dates):
            rows.append({"trade_date": d, "symbol": sym,
                         "close": px[i], "adj_close": px[i], "amount": amt[i]})
    return pd.DataFrame(rows), dates


def test_picks_lowest_volatility():
    bars, dates = _bars()
    strat = LowVolatility(top_n=1, window=60)
    t = strat.generate_target(dates[-1], bars, ["STABLE", "MID", "WILD", "ILLIQ"])
    assert list(t.weights) == ["STABLE"], f"应选 STABLE，实际 {list(t.weights)}"


def test_liquidity_filter_drops_illiquid_names():
    """成交额最低的标的即使波动低，也不该进流动性过滤后的组合。"""
    bars, dates = _bars(n=120)
    # 造一只波动极低但成交额极小的标的：不筛时应入选，筛掉后不应入选
    liq = LowVolatility(top_n=1, window=60, liquidity_pct=0.5)
    t = liq.generate_target(dates[-1], bars, ["STABLE", "MID", "WILD", "ILLIQ"])
    assert "ILLIQ" not in t.weights
    assert list(t.weights) == ["STABLE"]


def test_no_lookahead_future_bars_do_not_change_pick():
    """把未来的行情也传进去（含一次暴跌），选股结果必须不变。"""
    bars, dates = _bars()
    t1 = LowVolatility(top_n=1, window=60).generate_target(
        dates[-1], bars, ["STABLE", "MID", "WILD", "ILLIQ"])

    future = bars[bars["symbol"] == "STABLE"].copy()
    future["trade_date"] = dates[-1] + pd.Timedelta(days=1)
    future["adj_close"] = future["adj_close"] * 0.1        # 未来暴跌
    with_future = pd.concat([bars, future], ignore_index=True)
    t2 = LowVolatility(top_n=1, window=60).generate_target(
        dates[-1], with_future, ["STABLE", "MID", "WILD", "ILLIQ"])

    assert list(t1.weights) == list(t2.weights)


def test_returns_empty_when_history_too_short():
    bars, dates = _bars(n=30)
    t = LowVolatility(top_n=5, window=60).generate_target(dates[-1], bars, ["STABLE"])
    assert t.weights == {}
    assert LowVolatility(top_n=5, window=60).warmup == 61


def test_rejects_bad_liquidity_pct():
    for bad in (0, -0.1, 1.5):
        try:
            LowVolatility(liquidity_pct=bad)
        except ValueError:
            continue
        raise AssertionError(f"liquidity_pct={bad} 应当报错")
