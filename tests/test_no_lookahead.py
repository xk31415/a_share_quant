"""未来函数（look-ahead bias）专项测试。

这是整个项目最重要的测试：故意构造“未来数据被篡改”的场景，
验证信号、股票池、成交与财务数据都不会用到未来信息。
"""
import pandas as pd
import pytest

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.data.asof import latest_known
from a_share_quant.factors import Momentum
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies import Strategy, TargetPortfolio
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_bars, make_provider


# ---------- 1) 因子不能读取未来日期 ----------

def test_factor_values_unchanged_when_future_mutated():
    dates = pd.bdate_range("2021-01-04", periods=30)
    bars = make_bars(dates, ["AAA"])
    t = dates[19]
    factor = Momentum(window=5)
    v_before = factor.compute(bars).loc[t, "AAA"]

    # 篡改 t 之后的全部数据（x10）
    bars_future = bars.copy()
    future = pd.to_datetime(bars_future["trade_date"]) > t
    bars_future.loc[future, ["close", "adj_close", "open", "high", "low"]] *= 10.0

    v_after = factor.compute(bars_future).loc[t, "AAA"]
    assert v_after == v_before


def test_compute_as_of_equals_full_compute_on_that_date():
    dates = pd.bdate_range("2021-01-04", periods=30)
    bars = make_bars(dates, ["AAA", "BBB"])
    factor = Momentum(window=3)
    t = dates[20]
    as_of_series = factor.compute_as_of(bars, t)
    full = factor.compute(bars).loc[t]
    pd.testing.assert_series_equal(as_of_series, full, check_names=False)


# ---------- 2) 信号不能使用未来价格；成交价不能早于信号 ----------

def test_execution_uses_next_day_open_not_signal_day_close():
    dates = pd.bdate_range("2021-01-04", periods=8)
    bars = make_bars(dates, ["AAA"])
    provider = make_provider(dates, ["AAA"], bars=bars)
    engine = BacktestEngine(
        provider=provider,
        strategy=_EqualOne(),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=1, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0),
        config=BacktestConfig(start_date=str(dates[0].date()),
                              end_date=str(dates[-1].date()),
                              rebalance_frequency="W"))
    result = engine.run()
    first = result.trades.iloc[0]
    # 信号日 2021-01-08（周五），成交必须是 01-11（周一）开盘价，而非信号日收盘价
    assert first["trade_date"] == pd.Timestamp("2021-01-11")
    next_open = float(
        bars.loc[bars["trade_date"] == pd.Timestamp("2021-01-11"), "open"].iloc[0])
    assert first["price"] == pytest.approx(next_open)


class _EqualOne(Strategy):
    """只买 AAA 一只、满仓的极简策略。"""

    name = "equal_one"

    def generate_target(self, as_of, bars, universe):
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(),
                               weights={"AAA": 1.0})


def test_engine_never_passes_future_bars_to_strategy():
    dates = pd.bdate_range("2021-01-04", periods=15)
    provider = make_provider(dates, ["AAA", "BBB"])
    recorder = _RecordingStrategy()

    engine = BacktestEngine(
        provider=provider,
        strategy=recorder,
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=2, max_weight_per_stock=0.5),
        cost_model=CostModel(),
        config=BacktestConfig(start_date=str(dates[0].date()),
                              end_date=str(dates[-1].date()),
                              rebalance_frequency="W"))
    engine.run()
    assert recorder.calls, "strategy was never invoked"
    for as_of, max_date in recorder.calls:
        assert max_date <= as_of, f"strategy saw data beyond as_of: {max_date} > {as_of}"


class _RecordingStrategy(Strategy):
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def generate_target(self, as_of, bars, universe):
        max_date = pd.to_datetime(bars["trade_date"]).max()
        self.calls.append((pd.Timestamp(as_of).normalize(), max_date))
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights={})


# ---------- 3) 财务数据：公告日之前不可用 ----------

def test_latest_known_excludes_unannounced_fundamentals():
    fundamentals = pd.DataFrame([
        {"symbol": "AAA", "announce_date": "2021-01-10", "report_date": "2020-12-31", "eps": 1.0},
        {"symbol": "AAA", "announce_date": "2021-02-10", "report_date": "2020-12-31", "eps": 1.2},
        {"symbol": "AAA", "announce_date": "2021-04-25", "report_date": "2021-03-31", "eps": 0.5},
    ])
    # 2021-03-01 时点：只能看到 2021-02-10 公告的修正值
    out = latest_known(fundamentals, "2021-03-01", date_col="announce_date")
    assert len(out) == 1
    assert out["eps"].iloc[0] == 1.2
    # 2021-01-05 时点：什么都看不到
    assert latest_known(fundamentals, "2021-01-05", date_col="announce_date").empty


# ---------- 4) 股票池不能使用未来信息 ----------

def test_universe_unaffected_by_future_status_changes():
    dates = pd.bdate_range("2021-01-04", periods=40)
    symbols = ["AAA", "BBB"]
    status = pd.DataFrame([
        {"trade_date": d, "symbol": s, "is_st": False, "is_suspended": False}
        for d in dates for s in symbols
    ])
    t = dates[19]
    # as_of 之后 BBB 才变 ST —— 不得影响 t 时点的股票池
    status.loc[pd.to_datetime(status["trade_date"]) > t, "is_st"] = (
        status.loc[pd.to_datetime(status["trade_date"]) > t, "symbol"] == "BBB")

    provider = make_provider(dates, symbols, status=status)
    builder = UniverseBuilder(provider, UniverseRules(min_list_days=0))
    assert set(builder.get_universe(t)) == {"AAA", "BBB"}   # t 时点不受影响
    assert "BBB" not in builder.get_universe(dates[30])     # ST 生效后被剔除


def test_universe_unaffected_by_future_delist_dates():
    dates = pd.bdate_range("2021-01-04", periods=40)
    instruments = pd.DataFrame([
        {"symbol": "AAA", "name": "a",
         "list_date": dates[0] - pd.Timedelta(days=400), "delist_date": pd.NaT},
        {"symbol": "BBB", "name": "b",
         "list_date": dates[0] - pd.Timedelta(days=400), "delist_date": dates[35]},
    ])
    provider = make_provider(dates, ["AAA", "BBB"], instruments=instruments)
    builder = UniverseBuilder(provider, UniverseRules(min_list_days=0))
    t = dates[19]
    assert set(builder.get_universe(t)) == {"AAA", "BBB"}   # 退市发生在未来
    assert builder.get_universe(dates[38]) == ["AAA"]       # 退市后剔除
