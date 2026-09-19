"""因子计算正确性测试。"""
import pandas as pd
import pytest

from a_share_quant.factors import AvgAmount, Momentum, Volatility

from helpers import make_bars


def _bars(n=10):
    return make_bars(pd.bdate_range("2021-01-04", periods=n), ["AAA", "BBB"])


def test_momentum_matches_manual_calculation():
    bars = _bars()
    f = Momentum(window=3)
    panel = f.compute(bars)
    px = bars.loc[bars["symbol"] == "AAA"].set_index("trade_date")["adj_close"]
    expected = px / px.shift(3) - 1.0
    pd.testing.assert_series_equal(
        panel["AAA"], expected, check_names=False, check_freq=False)


def test_momentum_warmup_is_nan():
    panel = Momentum(window=5).compute(_bars(8))
    assert panel.iloc[:5].isna().all().all()
    assert panel.iloc[5:].notna().all().all()


def test_momentum_uses_adjusted_price():
    bars = _bars()
    bars["adj_close"] = bars["adj_close"] * 2.0  # 复权价不同于原始价
    panel = Momentum(window=3).compute(bars)
    px = bars.set_index(["trade_date"])  # noqa: F841
    adj = bars.loc[bars["symbol"] == "AAA"].set_index("trade_date")["adj_close"]
    expected = adj / adj.shift(3) - 1.0
    pd.testing.assert_series_equal(
        panel["AAA"], expected, check_names=False, check_freq=False)


def test_volatility_matches_manual_calculation():
    bars = _bars()
    f = Volatility(window=4)
    panel = f.compute(bars)
    px = bars.loc[bars["symbol"] == "BBB"].set_index("trade_date")["adj_close"]
    ret = px / px.shift(1) - 1.0
    expected = ret.rolling(4).std(ddof=1)
    pd.testing.assert_series_equal(
        panel["BBB"], expected, check_names=False, check_freq=False)


def test_avg_amount_matches_manual_calculation():
    bars = _bars()
    panel = AvgAmount(window=5).compute(bars)
    amt = bars.loc[bars["symbol"] == "AAA"].set_index("trade_date")["amount"]
    expected = amt.rolling(5).mean()
    pd.testing.assert_series_equal(
        panel["AAA"], expected, check_names=False, check_freq=False)


def test_factor_handles_suspension_gaps():
    """停牌/缺失数据：缺失行 pivot 后为 NaN，因子输出 NaN 而非报错。"""
    bars = _bars(10)
    drop_idx = bars.index[(bars["symbol"] == "AAA")
                          & (bars["trade_date"] >= bars["trade_date"].unique()[4])
                          & (bars["trade_date"] <= bars["trade_date"].unique()[6])]
    bars = bars.drop(index=drop_idx)
    panel = Momentum(window=2).compute(bars)
    assert panel["AAA"].isna().any()
    # BBB 完整不受影响
    assert panel["BBB"].notna().sum() == 8


def test_factor_invalid_window():
    with pytest.raises(ValueError):
        Momentum(window=0)
    with pytest.raises(ValueError):
        Volatility(window=1)
