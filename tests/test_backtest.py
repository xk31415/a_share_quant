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


def test_retried_sells_are_ordered_independent_of_hash_seed():
    """跌停卖不出的挂单，次日补挂顺序必须按代码排序。

    曾按 set 遍历顺序写回 pending_sells：顺序随 PYTHONHASHSEED 变，现金累加顺序跟着变，
    同一实验两次运行的 Sharpe/超额在浮点末位上不一致（EXP-20261022-151206-5368 复现）。
    """
    from a_share_quant.strategies import Strategy, TargetPortfolio

    syms = [f"S{i:02d}" for i in range(12)]
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, syms)
    d3 = pd.Timestamp(dates[2])
    mask = bars["trade_date"] == d3
    bars.loc[mask, "open"] = bars.loc[mask, "limit_down"]
    bars.loc[mask, "low"] = bars.loc[mask, "limit_down"]
    provider = make_provider(dates, syms, bars=bars)

    class _BuyThenExit(Strategy):
        name = "buy_then_exit"

        def __init__(self):
            self.n = 0

        def generate_target(self, as_of, bars, universe):
            self.n += 1
            w = {s: 1.0 / len(syms) for s in syms} if self.n == 1 else {}
            return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights=w)

    engine = BacktestEngine(
        provider=provider, strategy=_BuyThenExit(),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=len(syms), max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0),
        config=BacktestConfig(start_date=str(dates[0].date()),
                              end_date=str(dates[-1].date()),
                              rebalance_frequency="D"))
    trades = engine.run().trades
    day4 = trades[(trades["direction"] == "sell") & (trades["trade_date"] == dates[3])]
    retried = day4[day4["note"] == "blocked:no_available_shares"]
    assert len(retried) == len(syms)
    assert retried["symbol"].tolist() == sorted(syms)


# ---------- 停牌 ----------

def test_suspension_blocks_trading_from_status_table():
    """停牌以 status 表为权威来源：bars 完全没有 is_suspended 列时也必须拦截。

    这是历史缺陷的回归测试——旧实现只读 bars.is_suspended（可选列），
    导致按 schema 只提供 status 表的数据集里停牌约束静默失效。
    """
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA", "BBB"]).drop(columns=["is_suspended", "is_st"])
    d2 = pd.Timestamp(dates[1])
    status = pd.DataFrame([
        {"trade_date": d, "symbol": s,
         "is_st": False, "is_suspended": (d == d2 and s == "BBB")}
        for d in dates for s in ["AAA", "BBB"]])
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars, status=status)
    result = _engine(provider, freq="D", end=str(dates[-1].date())).run()
    bbb_d2 = result.trades[(result.trades["symbol"] == "BBB")
                           & (result.trades["trade_date"] == d2)]
    assert len(bbb_d2) >= 1
    assert (bbb_d2["note"] == "blocked:suspended").all()


def test_suspension_falls_back_to_bars_when_status_missing_row():
    """status 缺该交易日的记录时，bars 自带的 is_suspended 仍应生效（兜底）。"""
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA", "BBB"])
    d2 = pd.Timestamp(dates[1])
    bars.loc[(bars["trade_date"] == d2) & (bars["symbol"] == "BBB"), "is_suspended"] = True
    status = pd.DataFrame([
        {"trade_date": d, "symbol": s, "is_st": False, "is_suspended": False}
        for d in dates for s in ["AAA", "BBB"] if d != d2])
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars, status=status)
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


# ---------- 换手率口径 ----------

def test_turnover_is_one_way():
    """单边换手 = (买入额+卖出额)/2/权益；只买入时约等于买入额的一半。"""
    dates = pd.bdate_range("2021-01-04", "2021-01-15")
    provider = make_provider(dates, ["AAA", "BBB"])
    result = _engine(provider).run()
    d0 = result.turnover.index[0]
    assert result.buy_turnover.loc[d0] > 0
    assert result.sell_turnover.loc[d0] == 0 or pd.isna(result.sell_turnover.loc[d0])
    assert result.turnover.loc[d0] == pytest.approx(
        result.buy_turnover.loc[d0] / 2.0, rel=0.01)


# ---------- 现金不足时按比例缩放，而不是整笔丢弃 ----------

def test_cash_shortfall_scales_buys_instead_of_dropping():
    """目标权重合计 100% 且无现金缓冲时，所有买单按比例缩减，不得整笔丢弃。

    历史缺陷：买入按 symbol 升序消耗现金，末尾标的被 blocked:insufficient_cash
    整笔丢弃，造成与股票代码字典序相关的系统性偏差。
    """
    dates = pd.bdate_range("2021-01-04", "2021-02-26")
    syms = [f"S{i:02d}" for i in range(10)]
    provider = make_provider(dates, syms)
    engine = BacktestEngine(
        provider=provider,
        strategy=EqualWeightStrategy(max_positions=10),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=10, max_weight_per_stock=0.10),
        cost_model=CostModel(),                      # 含滑点，现金更紧
        config=BacktestConfig(start_date=str(dates[0].date()),
                              end_date=str(dates[-1].date()),
                              rebalance_frequency="M",
                              cash_buffer=0.0),      # 故意不预留缓冲
    )
    result = engine.run()
    assert "blocked:insufficient_cash" not in set(result.trades["note"])
    first_day = result.positions["trade_date"].min()
    held = result.positions[result.positions["trade_date"] == first_day]
    assert len(held) == 10
