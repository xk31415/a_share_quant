"""交易权限与数据契约的约束测试。

对应第三方审查（a_share_quant_REVIEW.md）指出的问题：
板块白名单、引擎强制股票池、停牌估值、逐行涨跌停缺失、成交量上限、
目标内缺行情的留痕。
"""
import pandas as pd
import pytest

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.backtest.execution import ExecutionSimulator, Order
from a_share_quant.backtest.positions import PortfolioState
from a_share_quant.data.validation import DataValidationError
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies import Strategy, TargetPortfolio
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_bars, make_provider

MAIN_BOARDS = ["600", "601", "603", "605", "000", "001", "002", "003", "300", "301"]


class Fixed(Strategy):
    name = "fixed"

    def __init__(self, weights):
        self._w = weights

    def generate_target(self, as_of, bars, universe):
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights=dict(self._w))


class Seq(Strategy):
    """按信号次序切换目标，用于构造"先建仓再变目标"的场景。"""

    name = "seq"

    def __init__(self, targets):
        self._targets = list(targets)
        self._n = 0

    def generate_target(self, as_of, bars, universe):
        w = self._targets[min(self._n, len(self._targets) - 1)]
        self._n += 1
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights=dict(w))


def _engine(provider, strategy, rules=None, **kw):
    return BacktestEngine(
        provider=provider, strategy=strategy,
        universe_builder=UniverseBuilder(provider, rules or UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=kw.pop("maxp", 2),
                                       max_weight_per_stock=kw.pop("maxw", 0.5)),
        cost_model=kw.pop("cost_model", CostModel(slippage_rate=0.0)),
        config=BacktestConfig(start_date=kw.pop("start", "2021-01-04"),
                              end_date=kw.pop("end", "2021-01-15"),
                              rebalance_frequency=kw.pop("freq", "D")),
        **kw)


# ---------- 板块白名单 ----------

def test_universe_board_prefix_whitelist_excludes_star_bse_b_share():
    dates = pd.bdate_range("2021-01-04", periods=10)
    symbols = ["600001", "000001", "300001", "688001", "689001",
               "830799", "430001", "920001", "200001", "900001", "000300"]
    provider = make_provider(dates, symbols)
    rules = UniverseRules(min_list_days=0, allowed_prefixes=MAIN_BOARDS,
                          excluded_symbols=["000300"])
    universe = UniverseBuilder(provider, rules).get_universe(dates[-1])
    assert set(universe) == {"600001", "000001", "300001"}


def test_missing_prefix_whitelist_emits_warning():
    dates = pd.bdate_range("2021-01-04", periods=10)
    provider = make_provider(dates, ["600001"])
    builder = UniverseBuilder(provider, UniverseRules(min_list_days=0))
    builder.get_universe(dates[-1])
    assert any("allowed_prefixes" in w for w in builder.warnings)


# ---------- 引擎强制股票池 ----------

def test_engine_blocks_target_outside_universe():
    """策略返回宇宙外标的时必须不下单，并留下可审计的违规记录。"""
    dates = pd.bdate_range("2021-01-04", periods=8)
    provider = make_provider(dates, ["688001", "600001"])
    rules = UniverseRules(min_list_days=0, allowed_prefixes=MAIN_BOARDS)
    engine = _engine(provider, Fixed({"688001": 1.0}), rules, freq="D",
                     end=str(dates[-1].date()))
    result = engine.run()
    assert result.trades.empty or (result.trades["symbol"] != "688001").all()
    assert result.universe_violations is not None
    assert set(result.universe_violations["symbol"]) == {"688001"}
    assert any("股票池外" in w for w in result.validation_warnings)


def test_engine_allows_target_inside_universe():
    dates = pd.bdate_range("2021-01-04", periods=8)
    provider = make_provider(dates, ["600001"])
    rules = UniverseRules(min_list_days=0, allowed_prefixes=MAIN_BOARDS)
    result = _engine(provider, Fixed({"600001": 1.0}), rules, freq="D",
                     end=str(dates[-1].date())).run()
    assert (result.trades["symbol"] == "600001").any()
    assert result.universe_violations.empty


# ---------- 停牌日估值 ----------

def test_suspended_day_does_not_repricing_equity():
    """停牌日即使数据商给了一根 bar，也不能用来估值（否则净值凭空跳变）。"""
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    d3 = pd.Timestamp(dates[2])
    m = bars["trade_date"] == d3
    for col in ("open", "high", "low", "close", "pre_close", "limit_up", "limit_down"):
        bars.loc[m, col] = bars.loc[m, col] * 2.0
    status = pd.DataFrame([{"trade_date": d, "symbol": "AAA", "is_st": False,
                            "is_suspended": (d == d3)} for d in dates])
    provider = make_provider(dates, ["AAA"], bars=bars, status=status)
    result = _engine(provider, Fixed({"AAA": 1.0}), freq="D",
                     end=str(dates[-1].date())).run()
    ret = result.daily_returns
    assert abs(ret.loc[d3]) < 0.01, f"停牌日收益应为 0 附近，实际 {ret.loc[d3]:.4f}"
    assert (result.trades[result.trades["trade_date"] == d3]["note"]
            == "blocked:suspended").all()


# ---------- 涨跌停逐行缺失 ----------

def test_single_row_nan_limit_raises():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    d3 = pd.Timestamp(dates[2])
    bars.loc[(bars["trade_date"] == d3), "limit_up"] = float("nan")
    provider = make_provider(dates, ["AAA"], bars=bars)
    with pytest.raises(DataValidationError, match="为空值"):
        _engine(provider, Fixed({"AAA": 1.0})).run()


def test_limit_exempt_allows_nan_and_skips_constraint():
    """无涨跌幅（注册制新股）必须显式标注 limit_exempt，而不是靠留空。"""
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    d3 = pd.Timestamp(dates[2])
    m = bars["trade_date"] == d3
    bars["limit_exempt"] = False
    bars.loc[m, "limit_exempt"] = True
    bars.loc[m, "limit_up"] = float("nan")
    bars.loc[m, "limit_down"] = float("nan")
    bars.loc[m, "open"] = 15.0        # 相对前收约 +50%，若无豁免必须被拦
    bars.loc[m, "high"] = 16.0
    bars.loc[m, "low"] = 9.0
    provider = make_provider(dates, ["AAA"], bars=bars)
    result = _engine(provider, Fixed({"AAA": 1.0}), freq="D",
                     end=str(dates[-1].date())).run()
    filled = result.trades[(result.trades["trade_date"] == d3)
                           & (result.trades["shares"] > 0)]
    assert not filled.empty


# ---------- 成交量上限 ----------

def test_volume_cap_limits_order_size():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    bars["volume"] = 10_000.0          # 全天 1 万股
    provider = make_provider(dates, ["AAA"], bars=bars)
    engine = _engine(provider, Fixed({"AAA": 1.0}), maxp=1, maxw=1.0, freq="D",
                     end=str(dates[-1].date()), max_participation_rate=0.10)
    result = engine.run()
    buys = result.trades[(result.trades["direction"] == "buy")
                         & (result.trades["shares"] > 0)]
    assert not buys.empty
    assert (buys["shares"] <= 1_000).all()          # 10% × 10,000
    assert buys["capped"].any()
    assert result.equity_curve.iloc[-1] > 0


def test_volume_cap_zero_blocks_illiquid_stock():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    bars["volume"] = 50.0               # 全天 50 股，不足一手
    provider = make_provider(dates, ["AAA"], bars=bars)
    engine = _engine(provider, Fixed({"AAA": 1.0}), maxp=1, maxw=1.0, freq="D",
                     end=str(dates[-1].date()), max_participation_rate=0.10)
    result = engine.run()
    assert "blocked:volume" in set(result.trades["note"])


# ---------- 目标内缺行情必须留痕 ----------

def test_target_without_bar_records_blocked():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["BBB"])            # 只有 BBB 的行情
    instruments = pd.DataFrame([
        {"symbol": s, "name": s, "list_date": dates[0] - pd.Timedelta(days=400),
         "delist_date": pd.NaT} for s in ("AAA", "BBB")])
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars, instruments=instruments)
    result = _engine(provider, Fixed({"AAA": 0.5, "BBB": 0.5}), freq="D",
                     end=str(dates[-1].date())).run()
    aaa = result.trades[result.trades["symbol"] == "AAA"]
    assert not aaa.empty, "目标内缺行情的标的必须留下 0 股记录"
    assert (aaa["note"] == "blocked:no_bar").all()
    assert (aaa["shares"] == 0).all()


def test_target_without_bar_direction_follows_intent():
    """已持仓 + 缺行情 -> 记卖单；未持仓 + 缺行情 -> 记买单（F-07）。"""
    dates = pd.bdate_range("2021-01-04", "2021-01-29")
    bars = make_bars(dates, ["AAA", "BBB"])
    missing = pd.Timestamp("2021-01-18")
    bars = bars.drop(index=bars.index[(bars.symbol == "AAA")
                                      & (bars.trade_date == missing)])
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars)
    engine = BacktestEngine(
        provider=provider,
        strategy=Seq([{"AAA": 1.0}, {"AAA": 0.01, "BBB": 0.99}]),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=2, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0),
        config=BacktestConfig("2021-01-04", "2021-01-29", "W"))
    result = engine.run()
    gap = result.trades[(result.trades.symbol == "AAA")
                        & (result.trades.trade_date == missing)]
    assert len(gap) == 1
    assert gap.iloc[0]["direction"] == "sell"
    assert gap.iloc[0]["note"] == "blocked:no_bar"


def test_volume_nan_blocks_when_cap_enabled():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    bars["volume"] = float("nan")
    provider = make_provider(dates, ["AAA"], bars=bars)
    with pytest.raises(DataValidationError, match="volume"):
        _engine(provider, Fixed({"AAA": 1.0}), max_participation_rate=0.10).run()


def test_sell_volume_cap_rounded_to_lot():
    """卖出上限也按整手取整：持有整百股时申报数量必须是 100 股整数倍（F-09）。"""
    sim = ExecutionSimulator(CostModel(commission_min=0.0, stamp_duty_rate=0.0,
                                       slippage_rate=0.0),
                             lot_size=100, max_participation_rate=0.10)
    state = PortfolioState(100_000)
    state.start_of_day()
    state.buy("600000", 1000, 10.0, 0.0)
    state.start_of_day()
    row = pd.DataFrame([{"symbol": "600000", "open": 10.0, "close": 10.0,
                         "volume": 2500.0, "limit_up": 11.0, "limit_down": 9.0,
                         "is_suspended": False}])
    fills = sim.execute([Order("600000", "sell", 500)], row, state)
    assert fills[0].shares == 200          # int(2500*0.1)=250 -> 向下取整到 200
    assert fills[0].capped is True
    assert state.positions["600000"].shares == 800


def test_cash_scaling_is_not_marked_capped():
    """现金不足造成的缩量不应记成成交量截断（F-13）。"""
    sim = ExecutionSimulator(CostModel(commission_min=0.0, stamp_duty_rate=0.0,
                                       slippage_rate=0.0),
                             lot_size=100, max_participation_rate=0.10)
    state = PortfolioState(5_000)
    row = pd.DataFrame([{"symbol": "600000", "open": 10.0, "close": 10.0,
                         "volume": 1_000_000.0, "limit_up": 11.0, "limit_down": 9.0,
                         "is_suspended": False}])
    fills = sim.execute([Order("600000", "buy", 2000)], row, state)
    assert fills[0].shares == 400          # 现金 5000 / 10
    assert fills[0].capped is False        # 上限 100,000 股，未绑定


def test_liquidity_filter_divides_by_trading_days():
    """流动性均值分母是交易日数：单日放量不能顶过阈值（F-11）。"""
    dates = pd.bdate_range("2021-01-04", periods=5)
    rows = [{"trade_date": d, "symbol": "FULL", "open": 10.0, "high": 10.5,
             "low": 9.5, "close": 10.0, "volume": 1e6, "amount": 100.0,
             "adj_close": 10.0, "limit_up": 11.0, "limit_down": 9.0}
            for d in dates]
    rows.append({"trade_date": dates[-1], "symbol": "GAP", "open": 10.0,
                 "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1e6,
                 "amount": 600.0, "adj_close": 10.0,
                 "limit_up": 11.0, "limit_down": 9.0})
    bars = pd.DataFrame(rows)
    provider = make_provider(dates, ["FULL", "GAP"], bars=bars)
    universe = UniverseBuilder(
        provider, UniverseRules(min_list_days=0, min_avg_daily_amount=200,
                                liquidity_lookback_days=5)).get_universe(dates[-1])
    assert universe == []      # GAP: 600/5=120 < 200；FULL: 100 < 200


def test_parquet_integer_symbol_rejected(tmp_path=None):
    """parquet 里 symbol 是数值类型 -> 直接报错，不让前导零静默丢失（F-12）。"""
    import tempfile
    from pathlib import Path

    from a_share_quant.data.loader import LocalFileProvider

    root = Path(tempfile.mkdtemp())
    dates = pd.bdate_range("2021-01-04", periods=3)
    bars = make_bars(dates, ["X"])
    bars["symbol"] = 1
    bars.to_parquet(root / "bars.parquet", index=False)
    pd.DataFrame([{"symbol": 1, "name": "a",
                   "list_date": pd.Timestamp("2020-01-01"),
                   "delist_date": pd.NaT}]).to_parquet(root / "instruments.parquet",
                                                       index=False)
    pd.DataFrame([{"trade_date": d, "symbol": 1, "is_st": False,
                   "is_suspended": False} for d in dates]).to_parquet(
        root / "status.parquet", index=False)
    pd.DataFrame({"trade_date": dates}).to_csv(root / "calendar.csv", index=False)

    with pytest.raises(ValueError, match="前导零"):
        LocalFileProvider(root).get_bars("2021-01-04", "2021-01-10")
