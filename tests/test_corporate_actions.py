"""公司行动（送转 / 现金分红）的估值测试（F-01 / D 组）。

核心断言：除权日**组合收益必须接近 0**——原始价跳空是账面现象，不是亏损。
这里刻意手工构造行情（不借用 helpers.make_bars 的随机漂移），
否则价格本身的日间变动会掩盖断言。
"""
import pandas as pd
import pytest

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies import Strategy, TargetPortfolio
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_provider


class BuyOne(Strategy):
    name = "buy_one"

    def generate_target(self, as_of, bars, universe):
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights={"AAA": 1.0})


class Half(Strategy):
    name = "half"

    def generate_target(self, as_of, bars, universe):
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights={"AAA": 0.5})


def _frame(dates, closes, adjs):
    """构造无漂移的行情：open=high=low=close，涨跌停按当日价 ±10%。"""
    rows, prev = [], closes[0]
    for d, c, a in zip(dates, closes, adjs):
        rows.append({"trade_date": d, "symbol": "AAA", "open": c, "high": c,
                     "low": c, "close": c, "volume": 1e6, "amount": c * 1e6,
                     "adj_close": a, "pre_close": prev,
                     "limit_up": c * 1.1, "limit_down": c * 0.9})
        prev = c
    return pd.DataFrame(rows)


def _run(bars, dates, strategy=None):
    provider = make_provider(dates, ["AAA"], bars=bars)
    engine = BacktestEngine(
        provider=provider, strategy=strategy or BuyOne(),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=1, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0, commission_min=0.0,
                             commission_rate=0.0, stamp_duty_rate=0.0),
        config=BacktestConfig(str(dates[0].date()), str(dates[-1].date()), "D"))
    return engine.run()


def test_split_does_not_create_fake_loss():
    """10 送 10：原始价腰斩、股数不变、adj 连续，市值不能跟着腰斩。"""
    dates = pd.bdate_range("2021-01-04", periods=6)
    closes = [10.0, 10.0, 5.0, 5.0, 5.0, 5.0]      # 第 3 天 10 送 10
    adjs = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]    # 后复权：连续
    result = _run(_frame(dates, closes, adjs), dates)

    ex = pd.Timestamp(dates[2])
    assert abs(result.daily_returns.loc[ex]) < 1e-9, \
        f"除权日收益应为 0，实际 {result.daily_returns.loc[ex]:.6f}"
    eq = result.equity_curve
    assert eq.loc[ex] == pytest.approx(eq.loc[pd.Timestamp(dates[1])], rel=1e-9)


def test_cash_dividend_does_not_create_fake_loss():
    """现金分红 5%：原始价跌 5%、adj 不变，净资产不减少。"""
    dates = pd.bdate_range("2021-01-04", periods=6)
    closes = [10.0, 10.0, 9.5, 9.5, 9.5, 10.0]
    adjs = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    result = _run(_frame(dates, closes, adjs), dates)

    ex = pd.Timestamp(dates[2])
    assert abs(result.daily_returns.loc[ex]) < 1e-9, \
        f"除息日收益应为 0，实际 {result.daily_returns.loc[ex]:.6f}"
    # 最后一天原始价与复权价同时上行 -> 正常体现为收益
    assert result.daily_returns.loc[pd.Timestamp(dates[5])] > 0


def _weight(result, date) -> float:
    """该日收盘时 AAA 的权重（组合非满仓，收益要按权重折算）。"""
    pos = result.positions
    row = pos[pos["trade_date"] == pd.Timestamp(date)]
    return float(row["weight"].sum()) if len(row) else 0.0


def test_market_move_after_split_is_measured_on_adjusted_scale():
    """除权后复权口径涨 2%，收益应体现为 2% × 持仓权重。"""
    dates = pd.bdate_range("2021-01-04", periods=5)
    closes = [10.0, 10.0, 5.0, 5.1, 5.1]
    adjs = [10.0, 10.0, 10.0, 10.2, 10.2]
    result = _run(_frame(dates, closes, adjs), dates)
    ret = result.daily_returns.loc[pd.Timestamp(dates[3])]
    w = _weight(result, dates[2])
    assert w > 0.9, "满仓策略的权重应接近 1"
    assert ret == pytest.approx(w * 0.02, abs=1e-9), f"实际 {ret:.6f}，权重 {w:.4f}"


def test_missing_adj_close_warns_explicitly():
    """没有 adj_close 时必须告警，不能静默按原始价估值。"""
    dates = pd.bdate_range("2021-01-04", periods=5)
    bars = _frame(dates, [10.0] * 5, [10.0] * 5).drop(columns=["adj_close"])
    result = _run(bars, dates)
    assert any("adj_close" in w for w in result.validation_warnings)


def test_half_position_split_keeps_valuation_consistent():
    """半仓 + 送转：现金不受影响，持仓市值连续。"""
    dates = pd.bdate_range("2021-01-04", periods=6)
    closes = [10.0, 10.0, 5.0, 5.0, 5.0, 5.0]
    adjs = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    result = _run(_frame(dates, closes, adjs), dates, strategy=Half())
    ex = pd.Timestamp(dates[2])
    assert abs(result.daily_returns.loc[ex]) < 1e-9


def test_no_action_data_behaves_like_raw_price():
    """没有公司行动（adj ≡ close）时，收益必须等于「原始价涨幅 × 持仓权重」。"""
    dates = pd.bdate_range("2021-01-04", periods=5)
    closes = [10.0, 10.0, 10.5, 10.5, 11.0]
    result = _run(_frame(dates, closes, closes), dates)
    for i in range(2, len(dates)):
        ratio = closes[i] / closes[i - 1]
        w = _weight(result, dates[i - 1])
        expected = w * (ratio - 1.0)
        got = result.daily_returns.loc[pd.Timestamp(dates[i])]
        assert got == pytest.approx(expected, abs=1e-9), \
            f"{dates[i].date()} 实际 {got:.6f}，按权重 {w:.4f} 预期 {expected:.6f}"
