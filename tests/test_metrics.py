"""绩效指标测试。"""
import math

import numpy as np
import pandas as pd
import pytest

from a_share_quant.metrics import (
    annual_returns,
    trim_warmup,
    annualized_return,
    annualized_volatility,
    average_holding_period,
    calmar_ratio,
    cost_contribution,
    cumulative_return,
    drawdown_curve,
    industry_exposure,
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


# ---------- 行业暴露 ----------

def _expo_positions(d1, d2):
    return pd.DataFrame([
        {"trade_date": d1, "symbol": "AAA", "shares": 100, "weight": 0.4},
        {"trade_date": d1, "symbol": "BBB", "shares": 100, "weight": 0.6},
        {"trade_date": d2, "symbol": "AAA", "shares": 100, "weight": 1.0},
    ])


def test_industry_exposure_static_map():
    d1, d2 = pd.Timestamp("2021-01-04"), pd.Timestamp("2021-01-05")
    pos = _expo_positions(d1, d2)
    imap = pd.Series({"AAA": "科技", "BBB": "消费"})
    expo = industry_exposure(pos, imap)
    assert expo.loc[d1, "科技"] == pytest.approx(0.4)
    assert expo.loc[d1, "消费"] == pytest.approx(0.6)
    assert expo.loc[d2, "科技"] == pytest.approx(1.0)
    assert expo.loc[d2, "消费"] == pytest.approx(0.0)


def test_industry_exposure_unknown_bucket_not_dropped():
    d1 = pd.Timestamp("2021-01-04")
    pos = pd.DataFrame([
        {"trade_date": d1, "symbol": "AAA", "shares": 100, "weight": 0.4},
        {"trade_date": d1, "symbol": "BBB", "shares": 100, "weight": 0.6},
    ])
    expo = industry_exposure(pos, pd.Series({"AAA": "科技"}))  # BBB 无分类
    assert "UNKNOWN" in expo.columns
    assert expo.loc[d1, "UNKNOWN"] == pytest.approx(0.6)
    # 总权重守恒
    assert float(expo.loc[d1].sum()) == pytest.approx(1.0)


def test_industry_exposure_historical_snapshot_no_lookahead():
    """行业分类变更时，历史日期必须使用当时已知的分类（防止未来函数）。"""
    d1, d2 = pd.Timestamp("2021-01-04"), pd.Timestamp("2021-06-01")
    pos = pd.DataFrame([
        {"trade_date": d1, "symbol": "AAA", "shares": 100, "weight": 1.0},
        {"trade_date": d2, "symbol": "AAA", "shares": 100, "weight": 1.0},
    ])
    hist = pd.DataFrame([
        {"trade_date": pd.Timestamp("2021-01-01"), "symbol": "AAA", "industry": "旧行业"},
        {"trade_date": pd.Timestamp("2021-05-01"), "symbol": "AAA", "industry": "新行业"},
    ])
    expo = industry_exposure(pos, hist)
    assert expo.loc[d1, "旧行业"] == pytest.approx(1.0)
    assert expo.loc[d1, "新行业"] == pytest.approx(0.0)
    assert expo.loc[d2, "新行业"] == pytest.approx(1.0)
    assert expo.loc[d2, "旧行业"] == pytest.approx(0.0)


def test_industry_exposure_ignores_zero_share_rows():
    d1 = pd.Timestamp("2021-01-04")
    pos = pd.DataFrame([
        {"trade_date": d1, "symbol": "AAA", "shares": 100, "weight": 0.5},
        {"trade_date": d1, "symbol": "BBB", "shares": 0, "weight": 0.0},
    ])
    expo = industry_exposure(pos, pd.Series({"AAA": "科技", "BBB": "消费"}))
    # 已清仓（shares=0）的行业不产生暴露列
    assert "消费" not in expo.columns
    assert expo.loc[d1, "科技"] == pytest.approx(0.5)


def test_industry_exposure_empty_input():
    assert industry_exposure(pd.DataFrame(), pd.Series(dtype=object)).empty


# ---------- 交易成本贡献 ----------

class _FakeResult:
    def __init__(self, equity, costs, turnover):
        self.equity_curve = equity
        self.daily_returns = equity.pct_change().fillna(0.0)
        self.costs = costs
        self.turnover = turnover
        self.positions = pd.DataFrame()
        self.trades = pd.DataFrame()
        self.benchmark = None


def test_cost_contribution_amounts():
    eq = _eq([100.0] * 253)  # 1 年（252 个区间），净值不变
    costs = pd.Series([100.0], index=[eq.index[100]])
    turnover = pd.Series([1.0], index=[eq.index[100]])
    cc = cost_contribution(_FakeResult(eq, costs, turnover))
    assert cc["total_costs"] == pytest.approx(100.0)
    assert cc["cost_pct_of_initial_capital"] == pytest.approx(1.0)
    assert cc["cost_pct_of_avg_equity"] == pytest.approx(1.0)
    # 成本 100 / 期初 100，一年 -> 年化拖累约 100%
    assert cc["annualized_cost_drag"] == pytest.approx(1.0, rel=1e-3)
    assert cc["n_cost_events"] == 1
    assert cc["avg_cost_per_rebalance"] == pytest.approx(100.0)
    # 成本/平均权益 = 100% ，单位换手成本 = 10000 bps
    assert cc["cost_bps_per_unit_turnover"] == pytest.approx(10000.0)


def test_cost_contribution_empty_costs():
    eq = _eq([100.0, 101.0])
    cc = cost_contribution(_FakeResult(eq, pd.Series(dtype=float), pd.Series(dtype=float)))
    assert cc["total_costs"] == 0.0
    assert cc["annualized_cost_drag"] == 0.0
    assert math.isnan(cc["cost_bps_per_unit_turnover"])


def test_cost_contribution_in_summarize():
    """成本归因必须进入汇总，避免只报告"最终收益率"。"""
    from a_share_quant.metrics import summarize

    eq = _eq([100.0] * 253)
    s = summarize(_FakeResult(eq, pd.Series([50.0], index=[eq.index[5]]),
                              pd.Series([1.0], index=[eq.index[5]])))
    assert "cost_pct_of_initial_capital" in s
    assert "annualized_cost_drag" in s
    assert s["total_costs"] == pytest.approx(50.0)
    assert s["annualized_cost_drag"] == pytest.approx(0.5, rel=1e-3)


# ---------------------------------------------------------------- 暖机期
def test_trim_warmup_drops_flat_prefix():
    """暖机期净值恒等于初始值，必须从年化/波动里截掉。

    不截的后果实测过：2015-2019 段前 78 个交易日空仓，同期中证1000
    涨 69.8%，策略记 0 收益，样本外超额被整体压低。
    """
    idx = pd.bdate_range("2024-01-01", periods=10)
    eq = pd.Series([1e6] * 4 + [1e6 * (1 + 0.01 * i) for i in range(1, 7)], index=idx)
    trimmed, n = trim_warmup(eq)
    # 平盘 4 个点，但最后那个平盘点要留作首个收益的基准，所以只截掉 3 个点
    assert n == 3
    assert len(trimmed) == 7
    # 关键：第 5 个点（+1%）的收益必须被算进来
    assert trimmed.pct_change().iloc[1] == pytest.approx(0.01, rel=1e-9)


def test_trim_warmup_prefers_positions():
    """有持仓表时以第一次持仓日为准，比"净值首次变动"更精确。"""
    idx = pd.bdate_range("2024-01-01", periods=10)
    eq = pd.Series([1e6] * 5 + [1e6 * (1 + 0.02) ** i for i in range(5)], index=idx)
    pos = pd.DataFrame({"trade_date": idx[3:], "shares": 100})
    trimmed, n = trim_warmup(eq, pos)
    # 切到首次持仓日的前一个净值点：那一天是首个收益的基准
    assert n == 2
    assert trimmed.index[0] == idx[2]
    assert trimmed.index[1] == idx[3]


def test_trim_warmup_no_flat_prefix_is_noop():
    idx = pd.bdate_range("2024-01-01", periods=5)
    eq = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0], index=idx)
    trimmed, n = trim_warmup(eq)
    assert n == 0
    assert len(trimmed) == 5


def test_trim_warmup_keeps_all_when_never_moves():
    """全程空仓时不截（否则序列为空，指标全 NaN）。"""
    idx = pd.bdate_range("2024-01-01", periods=5)
    eq = pd.Series([1e6] * 5, index=idx)
    trimmed, n = trim_warmup(eq)
    assert n == 0 and len(trimmed) == 5


def test_trim_warmup_keeps_first_position_day_return():
    """首次持仓日当天的收益必须进年化（R05 F-16）。

    引擎的净值序列在首次持仓日之前全是初始资金；切在首次持仓日当天会让
    当天从开盘到收盘的盈亏凭空消失，而 cumulative_return 用的是全序列、
    把它算了进去——两个指标自相矛盾。实测三段建仓日 +2.04%/+1.30%/+1.92%，
    年化因此低估 0.47/0.27/1.39pp。
    """
    idx = pd.bdate_range("2024-01-01", periods=5)
    eq = pd.Series([1e6, 1e6, 1e6 * 1.02, 1e6 * 1.02, 1e6 * 1.02], index=idx)
    pos = pd.DataFrame({"trade_date": idx[2:], "shares": 100})
    trimmed, n = trim_warmup(eq, pos)
    assert n == 1
    assert trimmed.index[0] == idx[1]          # 基准点 = 首次持仓日前一交易日
    assert trimmed.pct_change().iloc[1] == pytest.approx(0.02, rel=1e-9)


class _FakeResultWithPositions:
    def __init__(self, equity, positions):
        self.equity_curve = equity
        self.daily_returns = equity.pct_change().fillna(0.0)
        self.costs = pd.Series([0.0], index=[equity.index[0]])
        self.turnover = pd.Series([0.0], index=[equity.index[0]])
        self.positions = positions
        self.trades = pd.DataFrame()
        self.benchmark = None


def test_summarize_first_active_date_is_position_day_not_base_point():
    """first_active_date 必须报首次持仓日，不能报被保留的基准点（R05 F-16）。"""
    from a_share_quant.metrics import summarize

    idx = pd.bdate_range("2024-01-01", periods=10)
    eq = pd.Series([1e6] * 3 + [1e6 * 1.02] * 7, index=idx)
    pos = pd.DataFrame({"trade_date": idx[3:], "symbol": "000001",
                        "shares": 100, "weight": 1.0})
    s = summarize(_FakeResultWithPositions(eq, pos))
    assert s["first_active_date"] == str(idx[3].date())
    assert s["n_cash_only_days"] == 3
    assert s["warmup_days"] == 2               # 截掉的净值点数（基准点不算截）
    assert s["effective_days"] == 8
    assert s["annualized_return"] > 0.0        # 建仓日 +2% 必须进来
    assert s["cumulative_return"] == pytest.approx(0.02, rel=1e-9)


def test_summarize_no_duplicate_active_fields():
    """`*_active` 曾在截断后的序列上再算一遍，与主口径逐位相同（R05 F-18）。"""
    from a_share_quant.metrics import summarize

    idx = pd.bdate_range("2024-01-01", periods=10)
    eq = pd.Series([1e6] * 3 + [1e6 * (1.01 ** i) for i in range(1, 8)], index=idx)
    pos = pd.DataFrame({"trade_date": idx[3:], "symbol": "000001",
                        "shares": 100, "weight": 1.0})
    s = summarize(_FakeResultWithPositions(eq, pos))
    assert not [k for k in s if k.endswith("_active")]
