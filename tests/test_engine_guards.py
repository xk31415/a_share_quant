"""引擎启动阶段的守卫测试：数据校验、涨跌停列、暖机期。

这些测试锁定的都是"静默失效"类缺陷——配置或数据有问题时必须显式暴露，
而不是让回测悄悄跑出一个看起来正常的错误结果。
"""
import pandas as pd
import pytest

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.data.validation import DataValidationError
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies import EqualWeightStrategy, Strategy, TargetPortfolio
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_bars, make_provider


def _engine(provider, strategy=None, **kw):
    return BacktestEngine(
        provider=provider,
        strategy=strategy or EqualWeightStrategy(max_positions=2),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=2, max_weight_per_stock=0.5),
        cost_model=kw.pop("cost_model", CostModel(slippage_rate=0.0)),
        config=BacktestConfig(start_date=kw.pop("start", "2021-01-04"),
                              end_date=kw.pop("end", "2021-01-15"),
                              rebalance_frequency=kw.pop("freq", "W")),
        **kw)


# ---------- 涨跌停列缺失 ----------

def test_missing_limit_columns_raises_instead_of_silently_ignoring():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"]).drop(columns=["limit_up", "limit_down"])
    provider = make_provider(dates, ["AAA"], bars=bars)
    with pytest.raises(DataValidationError, match="limit_up"):
        _engine(provider).run()


def test_all_nan_limit_columns_raises():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    bars["limit_up"] = float("nan")
    bars["limit_down"] = float("nan")
    provider = make_provider(dates, ["AAA"], bars=bars)
    with pytest.raises(DataValidationError, match="limit_up"):
        _engine(provider).run()


def test_enforce_limit_false_allows_missing_limit_columns():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"]).drop(columns=["limit_up", "limit_down"])
    provider = make_provider(dates, ["AAA"], bars=bars)
    result = _engine(provider, enforce_limit=False).run()
    assert len(result.equity_curve) == len(dates)


# ---------- 引擎自带数据校验 ----------

def test_engine_runs_validation_without_script():
    """编程式调用（不经过 scripts/）也必须触发幸存者偏差哨兵。

    历史缺陷：validate_dataset 只在 run_baseline_backtest.py 里调用，
    QUICKSTART 教的编程式用法完全绕过数据校验。
    """
    dates = pd.bdate_range("2021-01-04", periods=200)
    rows = []
    for d in dates[:60]:
        rows.append({"trade_date": d, "symbol": "BBB", "open": 10.0, "high": 10.5,
                     "low": 9.5, "close": 10.0, "volume": 1e6, "amount": 1e7,
                     "adj_close": 10.0, "limit_up": 11.0, "limit_down": 9.0})
    for sym in ("AAA",):
        for d in dates:
            rows.append({"trade_date": d, "symbol": sym, "open": 10.0, "high": 10.5,
                         "low": 9.5, "close": 10.0, "volume": 1e6, "amount": 1e7,
                         "adj_close": 10.0, "limit_up": 11.0, "limit_down": 9.0})
    bars = pd.DataFrame(rows)
    instruments = pd.DataFrame([
        {"symbol": s, "name": s, "list_date": dates[0] - pd.Timedelta(days=365),
         "delist_date": pd.NaT} for s in ("AAA", "BBB")])
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars, instruments=instruments)
    result = _engine(provider, freq="M", end=str(dates[-1].date())).run()
    assert any("survivorship" in w for w in result.validation_warnings)


def test_validation_can_be_disabled_explicitly():
    dates = pd.bdate_range("2021-01-04", periods=6)
    provider = make_provider(dates, ["AAA"])
    result = _engine(provider, validate=False).run()
    # validate=False 关的是数据校验；股票池的运行期诊断（例如未配板块白名单）
    # 仍会保留，因此这里只断言"数据校验类"告警不再出现。
    assert not any("survivorship" in w or "NaN" in w
                   for w in result.validation_warnings)


# ---------- 暖机期 ----------

class _SlowStrategy(Strategy):
    name = "slow"
    warmup = 100

    def generate_target(self, as_of, bars, universe):
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights={})


def test_warmup_longer_than_window_warns():
    dates = pd.bdate_range("2021-01-04", periods=15)
    provider = make_provider(dates, ["AAA"])
    result = _engine(provider, strategy=_SlowStrategy(),
                     end=str(dates[-1].date())).run()
    assert any(w.startswith("warmup:") for w in result.validation_warnings)


def test_warmup_within_window_is_silent():
    dates = pd.bdate_range("2021-01-04", periods=15)
    provider = make_provider(dates, ["AAA"])

    class _Ok(Strategy):
        name = "ok"
        warmup = 3

        def generate_target(self, as_of, bars, universe):
            return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights={})

    result = _engine(provider, strategy=_Ok(), end=str(dates[-1].date())).run()
    assert not any(w.startswith("warmup:") for w in result.validation_warnings)


# ---------- 基准 ----------

def test_benchmark_without_adj_close_falls_back_to_close():
    """bars 无 adj_close 列时加载基准不应抛 KeyError（旧实现的缺陷）。"""
    dates = pd.bdate_range("2021-01-04", periods=8)
    bars = make_bars(dates, ["AAA", "000300"]).drop(columns=["adj_close"])
    provider = make_provider(dates, ["AAA", "000300"], bars=bars)
    engine = BacktestEngine(
        provider=provider,
        strategy=EqualWeightStrategy(max_positions=1),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=1, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0),
        config=BacktestConfig(start_date=str(dates[0].date()),
                              end_date=str(dates[-1].date()),
                              rebalance_frequency="W", benchmark="000300"))
    result = engine.run()
    assert result.benchmark is not None
    assert result.benchmark.name == "000300"


# ---------------------------------------------------------------- R08 F-44
def test_momentum_top_n_follows_max_positions():
    """R08 F-44：动量的 `top_n` 必须跟 `--max-positions` 走。

    原来写死配置里的 50，于是 `--max-positions 10` 时动量仍选 50 只，再由
    `build_target_weights` 按「权重降序、同权重字典序」截到 10 只。动量是**等权**，
    所以截断完全按代码字典序——名次信息在截断那一步丢光（实测拿到 000000~000009）。

    低波分支一直用的是 `top_n=max_pos`，只有动量分支是例外，这里防它退回去。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_baseline_backtest.py").read_text(encoding="utf-8")

    i = src.index('elif args.strategy == "momentum":')
    seg = src[i:src.index("else:", i)]
    assert "top_n=max_pos" in seg, "动量 top_n 必须跟 max_pos"
    assert 'top_n=pf.get("max_positions"' not in seg, \
        "不能再写死配置里的 max_positions"


# ---------------------------------------------------------------- R08 F-44
def test_momentum_top_n_follows_max_positions():
    """R08 F-44：动量的 `top_n` 必须跟 `--max-positions` 走。

    原来写死配置里的 50，于是 `--max-positions 10` 时动量仍选 50 只，再由
    `build_target_weights` 按「权重降序、同权重字典序」截到 10 只。动量是**等权**，
    所以截断完全按代码字典序——名次信息在截断那一步丢光（实测拿到 000000~000009）。

    低波分支一直用的是 `top_n=max_pos`，只有动量分支是例外，这里防它退回去。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_baseline_backtest.py").read_text(encoding="utf-8")

    i = src.index('elif args.strategy == "momentum":')
    seg = src[i:src.index("else:", i)]
    assert "top_n=max_pos" in seg, "动量 top_n 必须跟 max_pos"
    assert 'top_n=pf.get("max_positions"' not in seg, \
        "不能再写死配置里的 max_positions"
