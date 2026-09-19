"""股票池 point-in-time 过滤测试（防幸存者偏差）。"""
import pandas as pd

from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_provider


def test_universe_filters_are_point_in_time():
    dates = pd.bdate_range("2021-01-04", periods=100)
    t = dates[99]
    instruments = pd.DataFrame([
        # 老股票：正常入池
        {"symbol": "OLD", "name": "老股",
         "list_date": dates[0] - pd.Timedelta(days=400), "delist_date": pd.NaT},
        # 次新股：上市不足 60 个交易日，剔除
        {"symbol": "NEW", "name": "次新",
         "list_date": dates[90], "delist_date": pd.NaT},
        # 已退市：delist_date <= as_of，剔除
        {"symbol": "DEAD", "name": "退市",
         "list_date": dates[0] - pd.Timedelta(days=400), "delist_date": dates[50]},
        # 未来才退市：as_of 时点仍可投资，保留
        {"symbol": "SOON", "name": "将退市",
         "list_date": dates[0] - pd.Timedelta(days=400),
         "delist_date": dates[-1] + pd.Timedelta(days=30)},
    ])
    symbols = ["OLD", "NEW", "DEAD", "SOON"]
    provider = make_provider(dates, symbols, instruments=instruments)
    builder = UniverseBuilder(provider, UniverseRules(min_list_days=60))
    universe = builder.get_universe(t)
    assert "OLD" in universe
    assert "SOON" in universe
    assert "NEW" not in universe
    assert "DEAD" not in universe


def test_universe_excludes_st_and_suspended():
    dates = pd.bdate_range("2021-01-04", periods=30)
    symbols = ["AAA", "BBB", "CCC"]
    status = pd.DataFrame([
        {"trade_date": d, "symbol": s, "is_st": s == "BBB", "is_suspended": s == "CCC"}
        for d in dates for s in symbols
    ])
    provider = make_provider(dates, symbols, status=status)
    builder = UniverseBuilder(provider, UniverseRules(min_list_days=0))
    universe = builder.get_universe(dates[-1])
    assert universe == ["AAA"]


def test_universe_min_list_days_zero_keeps_new_listing():
    dates = pd.bdate_range("2021-01-04", periods=10)
    instruments = pd.DataFrame([
        {"symbol": "FRESH", "name": "新股", "list_date": dates[5], "delist_date": pd.NaT},
    ])
    provider = make_provider(dates, ["FRESH"], instruments=instruments)
    builder = UniverseBuilder(provider, UniverseRules(min_list_days=0))
    assert builder.get_universe(dates[6]) == ["FRESH"]


def test_universe_liquidity_filter():
    dates = pd.bdate_range("2021-01-04", periods=30)
    bars = make_provider(dates, ["AAA", "BBB"]).get_bars(dates[0], dates[-1])
    bars.loc[bars["symbol"] == "BBB", "amount"] = 1.0  # 流动性极差
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars)
    builder = UniverseBuilder(
        provider, UniverseRules(min_list_days=0, min_avg_daily_amount=100_000,
                                liquidity_lookback_days=10))
    universe = builder.get_universe(dates[-1])
    assert universe == ["AAA"]


def test_universe_requires_data_for_reserved_filters():
    dates = pd.bdate_range("2021-01-04", periods=10)
    provider = make_provider(dates, ["AAA"])
    builder = UniverseBuilder(
        provider, UniverseRules(min_list_days=0, index_membership="000300"))
    try:
        builder.get_universe(dates[-1])
        raise AssertionError("should have raised NotImplementedError")
    except NotImplementedError:
        pass
