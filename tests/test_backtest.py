"""回测引擎测试：T+1、涨跌停、停牌、权重、净值、回撤。"""
import pandas as pd
import pytest

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.backtest.positions import (
    InsufficientShares,
    PortfolioState,
)
from a_share_quant.metrics import maximum_drawdown
from a_share_quant.portfolio import PortfolioRules, build_target_weights
from a_share_quant.strategies import EqualWeightStrategy
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_bars, make_provider


def _engine(provider, **kw):
    return BacktestEngine(
        provider=provider,
        strategy=EqualWeightStrategy(max_positions=kw.pop("max_positions", 2)),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(
            max_positions=kw.pop("max_positions", 2),
            max_weight_per_stock=kw.pop("max_weight", 0.5)),
        cost_model=kw.pop("cost_model", CostModel(slippage_rate=0.0)),
        config=BacktestConfig(
            start_date=str(kw.pop("start", "2021-01-04")),
            end_date=str(kw.pop("end", "2021-01-15")),
            rebalance_frequency=kw.pop("freq", "W")),
        **kw)


# ---------- T+1 ----------

def test_t_plus_one_same_day_sell_blocked():
    state = PortfolioState(100_000)
    state.start_of_day()
    state.buy("AAA", 1000, 10.0, 5.0)
    # 当日买入当日不可卖
    with pytest.raises(InsufficientShares):
        state.sell("AAA", 1000, 10.0, 0.0)
    # 次日开盘释放后可卖
    state.start_of_day()
    state.sell("AAA", 1000, 10.0, 0.0)
    assert "AAA" not in state.positions


def test_first_fill_executes_on_next_trading_day():
    dates = pd.bdate_range("2021-01-04", "2021-01-15")
    provider = make_provider(dates, ["AAA", "BBB"])
    result = _engine(provider).run()
    # 第一信号日 = 2021-01-08（第一周最后交易日），成交应在 2021-01-11（周一）
    assert result.trades.iloc[0]["trade_date"] == pd.Timestamp("2021-01-11")
    assert set(result.trades["symbol"]) == {"AAA", "BBB"}


# ---------- 涨跌停 ----------

def test_limit_up_blocks_buy():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA", "BBB"])
    d2 = pd.Timestamp(dates[1])
    mask = (bars["trade_date"] == d2) & (bars["symbol"] == "AAA")
    limit = float(bars.loc[mask, "limit_up"].iloc[0])
    bars.loc[mask, "open"] = limit           # 开盘即涨停
    bars.loc[mask, "high"] = limit
    bars.loc[mask, "low"] = float(bars.loc[mask, "close"].iloc[0]) * 0.99

    provider = make_provider(dates, ["AAA", "BBB"], bars=bars)
    result = _engine(provider, freq="D", end=str(dates[-1].date())).run()
    aaa_d2 = result.trades[(result.trades["symbol"] == "AAA")
                           & (result.trades["trade_date"] == d2)]
    assert len(aaa_d2) == 1
    assert aaa_d2["note"].iloc[0] == "blocked:limit_up"
    assert aaa_d2["shares"].iloc[0] == 0
    # 当日只有 BBB 建仓
    pos_d2 = result.positions[result.positions["trade_date"] == d2]
    assert set(pos_d2["symbol"]) == {"BBB"}


def test_limit_down_blocks_sell():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    # 时序：d1 信号买入 -> d2 成交买入 -> d2 收盘信号清仓 -> d3 开盘卖出（跌停受阻）
    d3 = pd.Timestamp(dates[2])
    mask = (bars["trade_date"] == d3) & (bars["symbol"] == "AAA")
    limit_down = float(bars.loc[mask, "limit_down"].iloc[0])
    bars.loc[mask, "open"] = limit_down
    bars.loc[mask, "low"] = limit_down
    bars.loc[mask, "high"] = float(bars.loc[mask, "close"].iloc[0]) * 1.01

    provider = make_provider(dates, ["AAA"], bars=bars)

    # 第一天满仓买入，之后清仓
    from a_share_quant.strategies import Strategy, TargetPortfolio

    class _BuyThenExit(Strategy):
        name = "buy_then_exit"

        def __init__(self):
            self.n = 0

        def generate_target(self, as_of, bars, universe):
            self.n += 1
            w = {"AAA": 1.0} if self.n == 1 else {}
            return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights=w)

    engine = BacktestEngine(
        provider=provider, strategy=_BuyThenExit(),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=1, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0),
        config=BacktestConfig(start_date=str(dates[0].date()),
                              end_date=str(dates[-1].date()),
                              rebalance_frequency="D"))
    result = engine.run()
    sell = result.trades[result.trades["direction"] == "sell"]
    # d3 跌停当日卖出受阻；次日（d4）重试成交
    d3_sell = sell[sell["trade_date"] == d3]
    assert len(d3_sell) == 1
    assert d3_sell["note"].iloc[0] == "blocked:limit_down"
    assert d3_sell["shares"].iloc[0] == 0
    assert (sell["trade_date"] > d3).any()


# ---------- 停牌 ----------

def test_suspension_blocks_trading():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA", "BBB"])
    d2 = pd.Timestamp(dates[1])
    bars.loc[(bars["trade_date"] == d2) & (bars["symbol"] == "BBB"), "is_suspended"] = True
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars)
    result = _engine(provider, freq="D", end=str(dates[-1].date())).run()
    bbb_d2 = result.trades[(result.trades["symbol"] == "BBB")
                           & (result.trades["trade_date"] == d2)]
    assert (bbb_d2["note"] == "blocked:suspended").all()


def test_no_bar_blocks_trading():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA", "BBB"])
    d2 = pd.Timestamp(dates[1])
    bars = bars.drop(index=bars.index[(bars["trade_date"] == d2) & (bars["symbol"] == "BBB")])
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars)
    result = _engine(provider, freq="D", end=str(dates[-1].date())).run()
    bbb_d2 = result.trades[(result.trades["symbol"] == "BBB")
                           & (result.trades["trade_date"] == d2)]
    assert (bbb_d2["note"] == "blocked:no_bar").all()


# ---------- 组合权重 ----------

def test_build_target_weights_caps_count_and_weight():
    raw = {f"S{i}": 0.2 for i in range(10)}  # 总权重 2.0
    out = build_target_weights(raw, PortfolioRules(max_positions=3,
                                                   max_weight_per_stock=0.1))
    assert len(out) == 3
    assert all(w == pytest.approx(0.1) for w in out.values())
    assert sum(out.values()) <= 1.0


def test_engine_respects_max_weight():
    dates = pd.bdate_range("2021-01-04", "2021-01-29")
    provider = make_provider(dates, ["AAA", "BBB", "CCC"])
    result = _engine(provider, max_positions=3, max_weight=0.3).run()
    # 约束在成交时点生效；成交后随行情漂移允许小幅超出（此处 <1%）
    assert result.positions["weight"].max() <= 0.3 + 0.005


# ---------- 净值与回撤 ----------

def test_equity_accounting_hand_computed():
    state = PortfolioState(100_000)
    state.start_of_day()
    state.buy("AAA", 1000, 10.0, 0.0)
    assert state.cash == pytest.approx(90_000)
    assert state.market_value({"AAA": 10.0}) == pytest.approx(100_000)
    assert state.market_value({"AAA": 11.0}) == pytest.approx(101_000)


def test_equity_curve_starts_at_initial_cash():
    dates = pd.bdate_range("2021-01-04", "2021-01-15")
    provider = make_provider(dates, ["AAA", "BBB"])
    result = _engine(provider, start=str(dates[0].date()),
                     end=str(dates[-1].date())).run()
    assert result.equity_curve.iloc[0] == pytest.approx(1_000_000)
    assert len(result.equity_curve) == len(dates)
    assert result.daily_returns.iloc[0] != result.daily_returns.iloc[0]  # NaN


def test_maximum_drawdown_known_sequence():
    eq = pd.Series([100.0, 120.0, 60.0, 90.0, 70.0],
                   index=pd.bdate_range("2021-01-04", periods=5))
    assert maximum_drawdown(eq) == pytest.approx(-0.5)


def test_trades_and_costs_recorded():
    dates = pd.bdate_range("2021-01-04", "2021-02-26")
    provider = make_provider(dates, ["AAA", "BBB"])
    result = _engine(provider).run()
    assert (result.trades["cost"] >= 0).all()
    assert result.costs.sum() > 0
    assert (result.turnover > 0).all()
