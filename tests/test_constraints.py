"""交易权限与数据契约的约束测试。

对应第三方审查（a_share_quant_REVIEW.md）指出的问题：
板块白名单、引擎强制股票池、停牌估值、逐行涨跌停缺失、成交量上限、
目标内缺行情的留痕。
"""
import pandas as pd
import pytest

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
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
