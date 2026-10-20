"""基准超额指标（F-06）。"""
import pandas as pd

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.metrics import summarize
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies import Strategy, TargetPortfolio
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_bars, make_provider


class Fixed(Strategy):
    name = "fixed"

    def generate_target(self, as_of, bars, universe):
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(),
                               weights={"AAA": 1.0})


def _run(with_benchmark=True):
    dates = pd.bdate_range("2024-01-02", periods=40)
    syms = ["AAA", "000300"] if with_benchmark else ["AAA"]
    bars = make_bars(dates, syms)
    provider = make_provider(dates, syms, bars=bars)
    engine = BacktestEngine(
        provider=provider, strategy=Fixed(),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=1, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0),
        config=BacktestConfig(str(dates[0].date()), str(dates[-1].date()), "W",
                              benchmark="000300" if with_benchmark else None))
    return engine.run()


def test_benchmark_metrics_are_reported():
    result = _run()
    assert result.benchmark is not None
    s = summarize(result)
    for key in ("benchmark_symbol", "benchmark_cumulative_return",
                "excess_cumulative_return", "tracking_error",
                "information_ratio", "beta"):
        assert key in s, f"缺少 {key}"
    assert s["benchmark_symbol"] == "000300"
    assert s["tracking_error"] >= 0


def test_no_benchmark_means_no_keys():
    """没有基准行情时不能凭空补 0，而是干脆不输出这些指标。"""
    s = summarize(_run(with_benchmark=False))
    assert "excess_cumulative_return" not in s
    assert "tracking_error" not in s


def test_benchmark_prefers_file_over_colliding_stock_symbol():
    """000852 既是中证1000 指数也是石化机械，必须先读 benchmark.csv。"""
    from a_share_quant.backtest.engine import BacktestEngine

    class _FakeProvider:
        def get_benchmark(self, code):
            idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
            return pd.Series([6000.0, 6060.0], index=idx, name=code)

    eng = BacktestEngine(provider=_FakeProvider(), strategy=None,
                         universe_builder=None, portfolio_rules=None,
                         cost_model=None,
                         config=BacktestConfig("2024-01-02", "2024-01-03", "M"))
    bars = pd.DataFrame({
        "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "symbol": ["000852", "000852"], "close": [10.0, 10.5]})
    series = eng._load_benchmark(bars, "000852")
    assert series.iloc[0] == 6000.0, "读到了个股价格，说明又按代码撞车了"
