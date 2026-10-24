"""热点 + 龙头：选股手算、缺数据告警、防未来函数。

数字都在测试里写死，不调用策略内部函数复算一遍再断言。
"""
from pathlib import Path

import pandas as pd
import pytest
import yaml

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies import EqualWeightStrategy, HotspotDragonhead
from a_share_quant.strategies.hotspot_dragonhead import (
    MISSING_INDUSTRY_WARNING,
    NO_AMOUNT_WARNING,
    STATIC_INDUSTRY_WARNING,
    static_industry_map,
)
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_provider


def _bars(specs, n=6, *, with_industry=True, with_amount=True,
          industry_at=None):
    """构造 window+1 根 K 线。收益 = 末日价 / 首日价 - 1，中间价保持 10。

    specs: (symbol, industry, ret, amount, st_days, susp_days)
    st_days / susp_days 是从 0 起的下标集合，标在那些天上。
    """
    dates = pd.bdate_range("2024-01-02", periods=n)
    rows = []
    for spec in specs:
        sym, ind, ret, amt = spec[:4]
        st_days = spec[4] if len(spec) > 4 else set()
        susp_days = spec[5] if len(spec) > 5 else set()
        for i, d in enumerate(dates):
            px = 10.0 if i < n - 1 else 10.0 * (1.0 + ret)
            label = industry_at(sym, i, ind) if industry_at is not None else ind
            row = {
                "trade_date": d,
                "symbol": sym,
                "close": px,
                "adj_close": px,
                "is_st": i in st_days,
                "is_suspended": i in susp_days,
            }
            if with_amount:
                row["amount"] = amt
            if with_industry:
                row["industry"] = label
            rows.append(row)
    return pd.DataFrame(rows), dates


# 热点行业手算（window 覆盖首尾）：
#   H1 收益 0.50 成交额 100；H2 0.30 / 500；H3 0.10 / 300
#   行业等权 = 0.30
#   相对强弱 0.20 / 0.00 / -0.20，秩 3 / 2 / 1
#   成交额秩 1 / 3 / 2
#   得分 4 / 5 / 3 → 第 1 名 H2，第 2 名 H1
_HOT = [
    ("H1", "HOT", 0.50, 100.0),
    ("H2", "HOT", 0.30, 500.0),
    ("H3", "HOT", 0.10, 300.0),
    ("C1", "COLD", 0.00, 9999.0),
    ("C2", "COLD", -0.05, 9999.0),
    ("C3", "COLD", -0.10, 9999.0),
]


def _strategy(**kw):
    params = dict(top_k=1, top_m=1, window=5, amount_window=5,
                  min_industry_members=3)
    params.update(kw)
    return HotspotDragonhead(**params)


def test_composite_score_prefers_balanced_leader_over_pure_momentum():
    bars, dates = _bars(_HOT)
    universe = [s[0] for s in _HOT]
    top1 = _strategy().generate_target(dates[-1], bars, universe)
    # 只看相对强弱会选 H1，只看成交额会选 H2；两者相加也是 H2。
    assert list(top1.weights) == ["H2"]
    assert abs(top1.weights["H2"] - 1.0) < 1e-12

    top2 = _strategy(top_m=2).generate_target(dates[-1], bars, universe)
    assert list(top2.weights) == ["H2", "H1"]
    assert all(abs(w - 0.5) < 1e-12 for w in top2.weights.values())
    assert "C1" not in top2.weights


def test_min_members_drops_tiny_sector_even_if_it_returned_more():
    specs = [
        ("T1", "TINY", 0.90, 100.0),
        ("T2", "TINY", 0.80, 100.0),
        *_HOT,
    ]
    bars, dates = _bars(specs)
    universe = [s[0] for s in specs]
    kept = _strategy(min_industry_members=3).generate_target(
        dates[-1], bars, universe)
    assert list(kept.weights) == ["H2"]

    # 门槛降到 2 时，TINY 等权 0.85 高于 HOT 的 0.30，龙头是 T1。
    tiny = _strategy(min_industry_members=2).generate_target(
        dates[-1], bars, universe)
    assert list(tiny.weights) == ["T1"]


def test_current_st_and_suspension_leave_the_pool_before_sector_rank():
    """ST 必须在算行业收益之前剔除。

    BOOM 只有 G 是正常交易，S 当日 ST 且收益 5.0。若把 S 算进行业样本，
    BOOM 会变成热点，入选的是 G。按规则 BOOM 不够 2 只，热点是 REAL，龙头是 R。
    R 在第一天戴过帽、信号日已经摘帽，不能用历史 ST 把它剔掉。
    """
    n = 6
    specs = [
        ("G", "BOOM", 0.05, 100.0, set(), set()),
        ("S", "BOOM", 5.00, 100.0, {n - 1}, set()),
        ("R", "REAL", 0.20, 200.0, {0}, set()),
        ("B", "REAL", 0.10, 100.0, set(), set()),
        ("C", "REAL", 0.00, 100.0, set(), set()),
        ("P", "REAL", 0.40, 800.0, set(), {n - 1}),
    ]
    bars, dates = _bars(specs, n=n)
    picked = _strategy(min_industry_members=2).generate_target(
        dates[-1], bars, [s[0] for s in specs])
    assert list(picked.weights) == ["R"]
    assert "G" not in picked.weights
    assert "S" not in picked.weights
    assert "P" not in picked.weights


def test_universe_and_unmapped_symbols_cannot_enter():
    specs = list(_HOT) + [("U", None, 0.99, 1.0e9)]
    bars, dates = _bars(specs)
    # 最强的 HOT 不在股票池里时，只能在池内的 COLD 里选。
    cold_only = _strategy().generate_target(
        dates[-1], bars, ["C1", "C2", "C3", "U"])
    assert list(cold_only.weights) == ["C1"]
    assert "U" not in cold_only.weights
    assert "H2" not in cold_only.weights


def test_missing_industry_returns_empty_and_warns_once():
    bars, dates = _bars(_HOT, with_industry=False)
    strat = _strategy()
    universe = [s[0] for s in _HOT]
    first = strat.generate_target(dates[-1], bars, universe)
    assert first.weights == {}
    assert strat.industry_source == "missing"
    assert strat.warnings == [MISSING_INDUSTRY_WARNING]
    strat.generate_target(dates[-1], bars, universe)
    assert strat.warnings == []


def test_static_map_used_when_bars_have_no_industry_column():
    bars, dates = _bars(_HOT, with_industry=False)
    ind = {s[0]: s[1] for s in _HOT}
    strat = _strategy(industry_map=ind)
    picked = strat.generate_target(dates[-1], bars, list(ind))
    assert list(picked.weights) == ["H2"]
    assert strat.industry_source == "static"
    assert strat.warnings == [STATIC_INDUSTRY_WARNING]


def test_bars_industry_overrides_static_map():
    """静态表把所有人放进同一个行业时，会选中 C1（收益 0.40）。

    bars 把 HOT / COLD 分开：HOT 等权 0.20 高于 COLD 的 0.00，
    行业内相对强弱第 1 是 H1（成交额相同，不再用成交额拉开）。
    """
    specs = [
        ("H1", "HOT", 0.30, 100.0),
        ("H2", "HOT", 0.20, 100.0),
        ("H3", "HOT", 0.10, 100.0),
        ("C1", "COLD", 0.40, 100.0),
        ("C2", "COLD", -0.20, 100.0),
        ("C3", "COLD", -0.20, 100.0),
    ]
    bars, dates = _bars(specs)
    strat = _strategy(industry_map={s[0]: "ONE" for s in specs})
    picked = strat.generate_target(dates[-1], bars, [s[0] for s in specs])
    assert list(picked.weights) == ["H1"]
    assert strat.industry_source == "bars"
    assert strat.warnings == []


def test_partial_bars_industry_does_not_backfill_from_static_map():
    """bars 没标行业的股票，不能用静态表补进热点。"""
    specs = [
        ("H1", "HOT", 0.30, 100.0),
        ("H2", "HOT", 0.20, 100.0),
        ("H3", "HOT", 0.10, 100.0),
        ("C1", None, 0.99, 100.0),
    ]
    bars, dates = _bars(specs)
    strat = _strategy(industry_map={"C1": "HOT", "H1": "HOT", "H2": "HOT", "H3": "HOT"})
    picked = strat.generate_target(dates[-1], bars, [s[0] for s in specs])
    assert list(picked.weights) == ["H1"]
    assert "C1" not in picked.weights


def test_future_price_and_industry_do_not_change_the_target():
    specs = [
        ("H1", "HOT", 0.30, 100.0),
        ("H2", "HOT", 0.20, 100.0),
        ("H3", "HOT", 0.10, 100.0),
        ("C1", "COLD", 0.40, 100.0),
        ("C2", "COLD", -0.20, 100.0),
        ("C3", "COLD", -0.20, 100.0),
    ]
    bars, dates = _bars(specs)
    as_of = dates[-1]
    future = bars[bars["symbol"].isin(["H1", "C1"])].groupby("symbol", sort=False).tail(1).copy()
    future["trade_date"] = as_of + pd.Timedelta(days=1)
    future.loc[future["symbol"] == "C1", "adj_close"] = 1000.0
    future.loc[future["symbol"] == "C1", "close"] = 1000.0
    future.loc[future["symbol"] == "H1", "industry"] = "COLD"
    leaked = pd.concat([bars, future], ignore_index=True)
    strat = _strategy()
    universe = [s[0] for s in specs]
    full = strat.generate_target(as_of, leaked, universe)
    trunc = strat.generate_target(as_of, bars, universe)
    assert dict(full.weights) == dict(trunc.weights) == {"H1": 1.0}


def test_industry_label_uses_the_last_known_row_not_the_first():
    """A 在信号日之前属于 COLD，信号日当天转入 HOT。必须用当天的行业。"""
    specs = [
        ("A", "COLD", 0.50, 500.0),
        ("B", "HOT", 0.10, 400.0),
        ("C", "HOT", 0.05, 100.0),
        ("D", "COLD", 0.00, 100.0),
        ("E", "COLD", 0.00, 100.0),
        ("F", "COLD", 0.00, 100.0),
    ]

    def _at(sym, i, default):
        if sym == "A" and i == 5:
            return "HOT"
        return default

    bars, dates = _bars(specs, industry_at=_at)
    picked = _strategy(min_industry_members=2, top_m=2).generate_target(
        dates[-1], bars, [s[0] for s in specs])
    assert list(picked.weights) == ["A", "B"]
    assert "D" not in picked.weights


def test_insufficient_history_returns_empty():
    bars, dates = _bars(_HOT, n=3)
    picked = _strategy().generate_target(dates[-1], bars, [s[0] for s in _HOT])
    assert picked.weights == {}


def test_blank_labels_and_row_order_do_not_change_the_hot_sector():
    """行业空串、空白和字符串 nan 都当缺失。行情打乱顺序也不能换龙头。

    bars 里 HOT 三只有真行业，COLD 三只标签是空的。静态表想把 C1 补进 HOT，
    但不能补：有逐日标签时不用静态表回填。
    """
    specs = [
        ("H1", "HOT", 0.50, 100.0),
        ("H2", "HOT", 0.30, 500.0),
        ("H3", "HOT", 0.10, 300.0),
        ("C1", "  ", 0.99, 100.0),
        ("C2", "nan", 0.99, 100.0),
        ("C3", "", 0.99, 100.0),
    ]
    bars, dates = _bars(specs)
    bars = bars.sample(frac=1.0, random_state=1).reset_index(drop=True)
    strat = _strategy(industry_map={s[0]: "HOT" for s in specs})
    picked = strat.generate_target(dates[-1], bars, [s[0] for s in specs])
    assert list(picked.weights) == ["H2"]
    assert strat.industry_source == "bars"
    assert "C1" not in picked.weights


def test_non_positive_amount_falls_back_to_relative_strength():
    bars, dates = _bars(_HOT)
    bars = bars.copy()
    bars["amount"] = 0.0
    strat = _strategy()
    picked = strat.generate_target(dates[-1], bars, [s[0] for s in _HOT])
    assert list(picked.weights) == ["H1"]
    assert strat.warnings == [NO_AMOUNT_WARNING]


def test_missing_amount_falls_back_to_relative_strength():
    bars, dates = _bars(_HOT, with_amount=False)
    strat = _strategy()
    picked = strat.generate_target(dates[-1], bars, [s[0] for s in _HOT])
    # 没有成交额时 H1 的相对强弱最高，不再让给 H2。
    assert list(picked.weights) == ["H1"]
    assert strat.warnings == [NO_AMOUNT_WARNING]


def test_max_positions_keeps_the_hotter_sector_not_the_smaller_symbol():
    specs = list(_HOT) + [
        ("A1", "WARM", 0.05, 100.0),
        ("W2", "WARM", 0.00, 100.0),
        ("W3", "WARM", -0.05, 100.0),
    ]
    bars, dates = _bars(specs)
    picked = _strategy(top_k=2, top_m=1, max_positions=1).generate_target(
        dates[-1], bars, [s[0] for s in specs])
    assert list(picked.weights) == ["H2"]


def test_tie_breaks_on_symbol_ascending():
    specs = [
        ("B", "HOT", 0.20, 100.0),
        ("A", "HOT", 0.20, 100.0),
        ("C", "HOT", 0.00, 100.0),
    ]
    bars, dates = _bars(specs)
    picked = _strategy().generate_target(dates[-1], bars, ["A", "B", "C"])
    assert list(picked.weights) == ["A"]


def test_invalid_parameters_raise():
    with pytest.raises(ValueError):
        HotspotDragonhead(top_k=0)
    with pytest.raises(ValueError):
        HotspotDragonhead(top_m=0)
    with pytest.raises(ValueError):
        HotspotDragonhead(window=0)
    with pytest.raises(ValueError):
        HotspotDragonhead(amount_window=0)
    with pytest.raises(ValueError):
        HotspotDragonhead(min_industry_members=1)
    with pytest.raises(ValueError):
        HotspotDragonhead(max_positions=0)


def test_defaults_match_settings_and_fit_the_single_name_cap():
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    hd = raw["hotspot_dragonhead"]
    strat = HotspotDragonhead()
    assert hd["rebalance"] == "daily_on_change"
    assert strat.rebalance_on_change is True
    assert strat.top_k == hd["top_k"] == 5
    assert strat.top_m == hd["top_m"] == 2
    assert strat.window == hd["window"] == 20
    assert strat.amount_window == hd["amount_window"] == 20
    assert strat.min_industry_members == hd["min_industry_members"] == 3
    assert strat.warmup == 21
    # 默认 10 只等权 = 单票上限，组合层不会把权重切成现金。
    cap = raw["portfolio"]["max_weight_per_stock"]
    assert abs(1.0 / (strat.top_k * strat.top_m) - cap) < 1e-12


def test_static_industry_map_drops_blank_labels():
    inst = pd.DataFrame({
        "symbol": ["600000", "000001", "300001", "688001"],
        "industry": ["银行", "  ", None, "计算机"],
    })
    assert static_industry_map(inst) == {"600000": "银行", "688001": "计算机"}
    assert static_industry_map(pd.DataFrame({"symbol": ["600000"]})) is None


def test_baseline_runner_exposes_hotspot_without_touching_live():
    root = Path(__file__).resolve().parents[1]
    src = (root / "scripts" / "run_baseline_backtest.py").read_text(encoding="utf-8")
    assert '"hotspot"' in src
    assert "HotspotDragonhead(" in src
    assert "static_industry_map" in src
    assert "max_positions=max_pos" in src
    assert "daily_on_change" in src
    assert 'freq = "D"' in src
    # 热点不读取配置里的月频作为自己的调仓频率
    assert "rebalance_frequency" not in src[src.index('elif args.strategy == "hotspot":'):
                                            src.index("else:", src.index('elif args.strategy == "hotspot":'))]
    live = (root / "scripts" / "live_auto.py").read_text(encoding="utf-8")
    assert "hotspot" not in live
    assert "HotspotDragonhead" not in live


def test_engine_records_missing_industry_warning_once():
    dates = pd.bdate_range("2024-01-01", "2024-02-15")
    provider = make_provider(dates, ["600000", "000001"])
    strat = HotspotDragonhead(top_k=1, top_m=1, window=5, amount_window=5)
    engine = BacktestEngine(
        provider=provider,
        strategy=strat,
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=10, max_weight_per_stock=0.10),
        cost_model=CostModel(slippage_rate=0.0),
        config=BacktestConfig(
            start_date=str(dates[0].date()),
            end_date=str(dates[-1].date()),
            rebalance_frequency="M"),
    )
    result = engine.run()
    hits = [w for w in result.validation_warnings if "缺少行业" in w]
    assert hits == [MISSING_INDUSTRY_WARNING]
    assert result.trades.empty
    # 配置写了月频，策略声明日频按需，引擎必须把这件事说出来。
    assert any("日频按需" in w for w in result.validation_warnings)


def _growth_bars(dates, specs):
    """specs: (symbol, industry, daily_growth, amount)。开高低收同一价格，涨跌停放宽。"""
    rows = []
    for i, d in enumerate(dates):
        for sym, ind, growth, amt in specs:
            px = round(10.0 * ((1.0 + growth) ** i), 4)
            prev = round(10.0 * ((1.0 + growth) ** max(i - 1, 0)), 4)
            rows.append({
                "trade_date": pd.Timestamp(d),
                "symbol": sym,
                "open": px, "high": px, "low": px, "close": px,
                "volume": 1_000_000.0, "amount": amt,
                "adj_close": px, "pre_close": prev,
                "limit_up": round(px * 1.10, 4),
                "limit_down": round(px * 0.90, 4),
                "is_suspended": False, "is_st": False,
                "industry": ind,
            })
    return pd.DataFrame(rows)


def _filled_dates(trades: pd.DataFrame) -> list:
    if trades is None or trades.empty:
        return []
    got = trades[trades["shares"] > 0]
    if got.empty:
        return []
    return sorted(pd.to_datetime(got["trade_date"]).dt.normalize().unique())


def _run(provider, strategy, dates, freq="M"):
    engine = BacktestEngine(
        provider=provider,
        strategy=strategy,
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=10, max_weight_per_stock=0.60),
        cost_model=CostModel(slippage_rate=0.0, commission_rate=0.0,
                             commission_min=0.0, stamp_duty_rate=0.0),
        config=BacktestConfig(
            start_date=str(pd.Timestamp(dates[0]).date()),
            end_date=str(pd.Timestamp(dates[-1]).date()),
            rebalance_frequency=freq,
            cash_buffer=0.0),
        lot_size=100,
        enforce_limit=True,
        enforce_suspension=True,
    )
    return engine.run()


def test_daily_on_change_holds_when_the_book_is_unchanged():
    """两只股票每天都入选，价格分道扬镳。按需只在第一次建仓成交。

    同一段行情若按日频权重再平衡，后面几天还会买卖。
    """
    dates = pd.bdate_range("2024-03-01", periods=8)
    specs = [("AA", "HOT", 0.03, 1.0e7), ("BB", "HOT", 0.00, 1.0e7)]
    bars = _growth_bars(dates, specs)
    provider = make_provider(dates, ["AA", "BB"], bars=bars)
    strat = HotspotDragonhead(top_k=1, top_m=2, window=2, amount_window=2,
                              min_industry_members=2)
    result = _run(provider, strat, dates, freq="M")
    filled = _filled_dates(result.trades)
    # window=2 需要 3 根 K 线：第 3 个交易日收盘出信号，第 4 个交易日开盘成交。
    assert filled == [pd.Timestamp(dates[3])], filled
    assert any("日频按需" in w for w in result.validation_warnings)

    plain = EqualWeightStrategy(max_positions=2)
    chased = _run(provider, plain, dates, freq="D")
    assert len(_filled_dates(chased.trades)) > 1


def test_daily_on_change_trades_when_the_leader_changes():
    """龙头从 AA 换成 BB 的下一交易日才再成交，中间价格在涨也不调。"""
    dates = pd.bdate_range("2024-04-01", periods=8)
    rows = []
    # 前 6 天 AA 稳步高于 BB；第 7 天（下标 6）BB 跳涨，2 日收益超过 AA。
    aa = [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4]
    bb = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 20.0, 20.2]
    for i, d in enumerate(dates):
        for sym, px, amt in (("AA", aa[i], 500.0), ("BB", bb[i], 100.0)):
            prev = aa[i - 1] if sym == "AA" and i else (bb[i - 1] if i else px)
            rows.append({
                "trade_date": pd.Timestamp(d),
                "symbol": sym,
                "open": px, "high": px, "low": px, "close": px,
                "volume": 1_000_000.0, "amount": amt,
                "adj_close": px, "pre_close": prev,
                "limit_up": round(px * 1.10, 4),
                "limit_down": round(px * 0.90, 4),
                "is_suspended": False, "is_st": False,
                "industry": "HOT",
            })
    bars = pd.DataFrame(rows)
    provider = make_provider(dates, ["AA", "BB"], bars=bars)
    strat = HotspotDragonhead(top_k=1, top_m=1, window=2, amount_window=2,
                              min_industry_members=2)
    # 信号日手算：下标 2..5 选 AA，下标 6 起 BB 的 2 日收益为 100%。
    assert list(strat.generate_target(dates[5], bars, ["AA", "BB"]).weights) == ["AA"]
    assert list(strat.generate_target(dates[6], bars, ["AA", "BB"]).weights) == ["BB"]
    result = _run(provider, strat, dates, freq="M")
    filled = _filled_dates(result.trades)
    # 下标 2 收盘信号 → 下标 3 买入 AA；下标 6 收盘换成 BB → 下标 7 调仓。
    assert filled == [pd.Timestamp(dates[3]), pd.Timestamp(dates[7])], filled
    day7 = result.trades[(result.trades["trade_date"] == dates[7])
                         & (result.trades["shares"] > 0)]
    assert set(zip(day7["symbol"], day7["direction"])) == {("AA", "sell"), ("BB", "buy")}
