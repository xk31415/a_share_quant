"""绩效指标测试。"""
import math

import numpy as np
import pandas as pd
import pytest

from a_share_quant.metrics import (
    annual_returns,
    annualized_return,
    annualized_volatility,
    average_holding_period,
    calmar_ratio,
    cumulative_return,
    drawdown_curve,
    maximum_drawdown,
    monthly_returns,
    sharpe_ratio,
    win_rate,
)


def _eq(values, start="2021-01-04"):
    return pd.Series(values, dtype=float,
                     index=pd.bdate_range(start, periods=len(values)))


def test_cumulative_return():
    assert cumulative_return(_eq([100, 150])) == pytest.approx(0.5)


def test_annualized_return_doubling_in_one_year():
    eq = _eq([100.0] * 252 + [200.0])  # 252 个交易日后翻倍
    assert annualized_return(eq) == pytest.approx(1.0)


def test_annualized_volatility_scales():
    rets = pd.Series([0.01, -0.01] * 126)  # 日波动约 1%
    vol = annualized_volatility(rets)
    assert vol == pytest.approx(0.01 * math.sqrt(252), rel=0.05)


def test_sharpe_ratio_sign_and_zero_vol():
    positive = pd.Series([0.001, 0.002, 0.001, 0.003, 0.001])
    assert sharpe_ratio(positive) > 0
    assert sharpe_ratio(pd.Series([0.01] * 10)) == 0.0  # 零波动


def test_drawdown_metrics():
    eq = _eq([100, 120, 60, 90, 70])
    assert maximum_drawdown(eq) == pytest.approx(-0.5)
    dd = drawdown_curve(eq)
    assert (dd <= 0).all()
    assert dd.iloc[-1] == pytest.approx(70 / 120 - 1)


def test_calmar_ratio():
    eq = _eq([100, 120, 60, 90, 70])
    assert calmar_ratio(eq) == pytest.approx(annualized_return(eq) / 0.5)
    assert math.isnan(calmar_ratio(_eq([100, 110, 120])))  # 无回撤 -> NaN


def test_win_rate():
    assert win_rate(pd.Series([0.01, -0.02, 0.03, 0.0])) == pytest.approx(2 / 3)


def test_annual_returns_grouping():
    idx = pd.bdate_range("2021-01-04", "2022-06-30")
    eq = pd.Series(np.linspace(100, 200, len(idx)), index=idx)
    ar = annual_returns(eq)
    assert set(ar.index) == {2021, 2022}
    expected_2021 = eq["2021"].iloc[-1] / eq.iloc[0] - 1
    assert ar.loc[2021] == pytest.approx(expected_2021)


def test_monthly_returns_grouping():
    idx = pd.bdate_range("2021-01-04", "2021-03-31")
    eq = pd.Series(np.linspace(100, 130, len(idx)), index=idx)
    mr = monthly_returns(eq)
    assert list(mr.index) == ["2021-01", "2021-02", "2021-03"]
    assert mr.iloc[0] == pytest.approx(eq["2021-01"].iloc[-1] / eq.iloc[0] - 1)


def test_average_holding_period():
    dates = pd.DatetimeIndex(pd.bdate_range("2021-01-04", periods=10))
    positions = pd.DataFrame([
        {"trade_date": d, "symbol": "AAA", "shares": 100}
        for d in dates[:5]
    ] + [
        {"trade_date": d, "symbol": "BBB", "shares": 100}
        for d in dates[2:7]
    ])
    # AAA 连续 5 天，BBB 连续 5 天 -> 平均 5
    assert average_holding_period(positions) == pytest.approx(5.0)
    assert math.isnan(average_holding_period(pd.DataFrame()))
