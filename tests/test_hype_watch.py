"""炒作苗头观察池：规则可证伪、不看未来、paper only、断路与 T+1 撮合。"""
from pathlib import Path

import pandas as pd
import pytest
import yaml

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies.hype_watch import (
    HypeWatchParams,
    HypeWatchStrategy,
    build_observations,
    load_theme_map_csv,
    load_watchlist_csv,
    normalize_theme_map,
    normalize_watchlist,
    paper_summary,
    runtime_overrides,
)
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_provider


ROOT = Path(__file__).resolve().parents[1]


def _params(**kw) -> HypeWatchParams:
    base = dict(
        circuit_enabled=False,
        min_limitup_count=3,
        min_theme_members=3,
        min_theme_excess=0.01,
        pool_size=3,
        max_themes=3,
        gross_exposure=1.0,
        position_mode="equal",
    )
    base.update(kw)
    return HypeWatchParams(**base)


def _bench(dates, level=1000.0) -> pd.Series:
    return pd.Series(float(level), index=pd.DatetimeIndex(dates), name="000300")


def _history(symbols, n=4, limit_symbols=(), theme_of=None, amount_of=None,
             st=(), suspended=(), limit_ratio=None, close_ratio=None):
    """最后一天让 limit_symbols 收在涨停价，其余小涨。涨停价按每只自己的比例，不写死 10%。"""
    dates = pd.bdate_range("2024-01-02", periods=n)
    theme_of = theme_of or {}
    amount_of = amount_of or {}
    limit_ratio = limit_ratio or {}
    close_ratio = close_ratio or {}
    px = {s: 10.0 for s in symbols}
    rows = []
    for i, d in enumerate(dates):
        for s in symbols:
            pre = px[s]
            ratio = float(limit_ratio.get(s, 0.10))
            lu = round(pre * (1.0 + ratio), 2)
            ld = round(pre * (1.0 - ratio), 2)
            if i == n - 1 and s in limit_symbols:
                cr = close_ratio.get(s, 1.0)
                close = round(lu * cr, 2) if cr != 1.0 else lu
            else:
                close = round(pre * 1.001, 4)
            open_px = pre
            rows.append({
                "trade_date": pd.Timestamp(d),
                "symbol": s,
                "open": open_px,
                "high": max(open_px, close),
                "low": min(open_px, close),
                "close": close,
                "volume": 1_000_000.0,
                "amount": float(amount_of.get(s, 1e8)),
                "adj_close": close,
                "pre_close": pre,
                "limit_up": lu,
                "limit_down": ld,
                "limit_exempt": False,
                "is_st": s in st,
                "is_suspended": s in suspended,
                "theme": theme_of.get(s, "AI"),
            })
            px[s] = close
    return pd.DataFrame(rows), dates


def _run(bars, dates, universe, bench=None, theme_map=None, watchlist=None, **kw):
    strat = HypeWatchStrategy(
        params=_params(**kw),
        benchmark=_bench(dates) if bench is None else bench,
        theme_map=theme_map,
        watchlist=watchlist)
    target = strat.generate_target(dates[-1], bars, universe)
    return strat, target


# ---------------------------------------------------------------- 规则

def test_three_limit_ups_trigger_equal_weight_pool():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    strat, target = _run(bars, dates, symbols)
    assert set(target.weights) == set(symbols)
    assert abs(sum(target.weights.values()) - 1.0) < 1e-12
    assert all(r["mode"] == "paper" and r["label"] == "paper/观察仿真"
               for r in strat.signal_log)
    assert all("T+1" in r["execution_assumption"] for r in strat.signal_log)


def test_two_limit_ups_do_not_trigger():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=["600001", "600002"])
    strat, target = _run(bars, dates, symbols)
    assert target.weights == {}
    row = next(r for r in strat.signal_log if r["kind"] == "theme")
    assert row["triggered"] is False
    assert row["reason"] == "limitup_below_threshold"
    assert int(row["n_limitup"]) == 2


def test_limit_up_uses_limit_price_not_a_fixed_10_percent():
    """创业板涨停价是 20% 时，涨 10% 不算涨停。不拿 pre_close*10% 去数家数。"""
    symbols = ["600001", "600002", "300001"]
    bars, dates = _history(
        symbols,
        limit_symbols=symbols,
        limit_ratio={"300001": 0.20},
        close_ratio={"300001": 11.0 / 12.0},  # 10 的 1.10，低于 12 的涨停价
    )
    # 300001 的涨停价 = round(10*1.2, 2)=12，收盘被乘成 11，不是涨停。
    last = bars[(bars["symbol"] == "300001") & (bars["trade_date"] == dates[-1])].iloc[0]
    assert float(last["close"]) < float(last["limit_up"]) - 1e-6
    strat, target = _run(bars, dates, symbols)
    assert target.weights == {}
    assert int(next(r for r in strat.signal_log if r["kind"] == "theme")["n_limitup"]) == 2

    bars2, dates2 = _history(
        symbols, limit_symbols=symbols, limit_ratio={"300001": 0.20})
    _, target2 = _run(bars2, dates2, symbols)
    assert "300001" in target2.weights


def test_excess_below_threshold_does_not_trigger():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=symbols, limit_ratio={s: 0.005 for s in symbols})
    # 涨停价只比昨收高 0.5%，三只都封板，但超额只有约 0.5% < 1%。
    strat, target = _run(bars, dates, symbols, min_theme_excess=0.01)
    assert target.weights == {}
    row = next(r for r in strat.signal_log if r["kind"] == "theme")
    assert row["reason"] == "excess_below_threshold"
    assert float(row["theme_excess"]) < 0.01


def test_excess_at_threshold_triggers():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    _, target = _run(bars, dates, symbols, min_theme_excess=0.01)
    assert set(target.weights) == set(symbols)


def test_missing_limit_price_blocks_the_theme():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    mask = (bars["symbol"] == "600003") & (bars["trade_date"] == dates[-1])
    bars.loc[mask, "limit_up"] = float("nan")
    strat, target = _run(bars, dates, symbols, min_limitup_count=2, min_theme_members=2)
    assert target.weights == {}
    assert next(r for r in strat.signal_log if r["kind"] == "theme")["reason"] == "missing_limit"


def test_missing_limit_column_raises_instead_of_inventing_ten_percent():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    bars = bars.drop(columns=["limit_up"])
    strat = HypeWatchStrategy(params=_params(), benchmark=_bench(dates))
    with pytest.raises(ValueError, match="limit_up"):
        strat.generate_target(dates[-1], bars, symbols)


def test_st_and_suspended_do_not_count_or_enter_the_pool():
    symbols = ["600001", "600002", "600003", "600004"]
    bars, dates = _history(symbols, limit_symbols=symbols, st=("600004",))
    _, target = _run(bars, dates, symbols)
    assert "600004" not in target.weights
    assert set(target.weights) == {"600001", "600002", "600003"}

    bars2, dates2 = _history(
        ["600001", "600002", "600003"],
        limit_symbols=["600001", "600002", "600003"],
        suspended=("600003",))
    strat, target2 = _run(bars2, dates2, ["600001", "600002", "600003"])
    assert target2.weights == {}
    assert int(next(r for r in strat.signal_log if r["kind"] == "theme")["n_limitup"]) == 2


def test_star_and_bse_prefixes_never_enter_and_do_not_fill_the_count():
    symbols = ["600001", "600002", "688001"]
    bars, dates = _history(symbols, limit_symbols=symbols, theme_of={s: "AI" for s in symbols})
    strat, target = _run(bars, dates, symbols, min_limitup_count=3)
    assert target.weights == {}
    row = next(r for r in strat.signal_log if r["kind"] == "theme")
    assert int(row["n_limitup"]) == 2
    assert int(row["n_excluded_board"]) >= 1

    symbols2 = ["600001", "600002", "688001", "830001", "689001", "430001"]
    bars2, dates2 = _history(symbols2, limit_symbols=["600001", "600002"])
    _, target2 = _run(bars2, dates2, symbols2, min_limitup_count=2, min_theme_members=2)
    assert set(target2.weights) == {"600001", "600002"}
    for banned in ("688001", "830001", "689001", "430001"):
        assert banned not in target2.weights


def test_index_code_000300_is_excluded_even_if_the_caller_puts_it_in_the_universe():
    symbols = ["600001", "600002", "600003", "000300"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    _, target = _run(bars, dates, symbols)
    assert "000300" not in target.weights
    assert set(target.weights) == {"600001", "600002", "600003"}


def test_round_robin_across_themes_and_amount_rank_inside_a_theme():
    ai = ["600001", "600002", "600003"]
    chip = ["000001", "000002", "001001"]
    symbols = ai + chip
    theme = {s: "AI" for s in ai} | {s: "CHIP" for s in chip}
    amount = {"600001": 30, "600002": 20, "600003": 10,
              "000001": 90, "000002": 80, "001001": 70}
    bars, dates = _history(symbols, limit_symbols=symbols, theme_of=theme, amount_of=amount)
    _, target = _run(bars, dates, symbols, pool_size=3, max_themes=2)
    # 超额与家数打平后题材名 AI 在前。轮转：AI 成交额最大、CHIP 最大、AI 次大。
    assert set(target.weights) == {"600001", "000001", "600002"}

    _, one = _run(bars, dates, ai, pool_size=1, max_themes=1)
    assert list(one.weights) == ["600001"]


def test_fixed_weight_scales_down_when_the_sum_would_exceed_gross():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    _, loose = _run(bars, dates, symbols, position_mode="fixed", fixed_weight=0.2,
                    gross_exposure=1.0)
    assert all(abs(w - 0.2) < 1e-12 for w in loose.weights.values())
    strat, tight = _run(bars, dates, symbols, position_mode="fixed", fixed_weight=0.5,
                        gross_exposure=1.0)
    assert abs(sum(tight.weights.values()) - 1.0) < 1e-12
    assert any("fixed_scaled_to_gross" in r["reason"] for r in strat.signal_log if r["selected"])


def test_no_theme_source_raises():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    bars = bars.drop(columns=["theme"])
    strat = HypeWatchStrategy(params=_params(), benchmark=_bench(dates))
    with pytest.raises(ValueError, match="themes.csv"):
        strat.generate_target(dates[-1], bars, symbols)


def test_missing_benchmark_raises_when_excess_gate_is_on():
    with pytest.raises(ValueError, match="基准"):
        HypeWatchStrategy(params=_params(min_theme_excess=0.01), benchmark=None)


def test_industry_column_is_only_a_proxy_and_warns():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    bars = bars.drop(columns=["theme"]).rename(columns={})
    bars["industry"] = "BANK"
    strat = HypeWatchStrategy(params=_params(), benchmark=_bench(dates))
    target = strat.generate_target(dates[-1], bars, symbols)
    assert set(target.weights) == set(symbols)
    assert any("industry" in w for w in strat.warnings)


# ---------------------------------------------------------------- 时间对齐

def test_future_prices_and_future_theme_do_not_change_todays_weights():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, n=6, limit_symbols=symbols)
    # 触发日取倒数第三天，后面还有行情。
    as_of = dates[-3]
    bars.loc[bars["trade_date"] == as_of, "close"] = bars.loc[bars["trade_date"] == as_of, "limit_up"]
    bars.loc[bars["trade_date"] == as_of, "adj_close"] = bars.loc[bars["trade_date"] == as_of, "close"]
    strat = HypeWatchStrategy(params=_params(), benchmark=_bench(dates))
    before = dict(strat.generate_target(as_of, bars, symbols).weights)
    mutated = bars.copy()
    future = mutated["trade_date"] > as_of
    mutated.loc[future, ["close", "adj_close", "open", "high", "low"]] *= 10.0
    mutated.loc[future, "theme"] = "OTHER"
    strat2 = HypeWatchStrategy(params=_params(), benchmark=_bench(dates))
    after = dict(strat2.generate_target(as_of, mutated, symbols).weights)
    assert before == after
    assert set(before) == set(symbols)


def test_theme_map_respects_start_and_end_dates():
    symbols = ["600001", "600002", "600003", "600004"]
    bars, dates = _history(symbols, limit_symbols=symbols)
    as_of = dates[-1]
    theme_map = pd.DataFrame([
        {"symbol": "600001", "theme": "AI", "start_date": dates[0], "end_date": pd.NaT},
        {"symbol": "600002", "theme": "AI", "start_date": dates[0], "end_date": pd.NaT},
        {"symbol": "600003", "theme": "AI", "start_date": dates[0], "end_date": pd.NaT},
        # 明天才加入，今天不能算进涨停家数。
        {"symbol": "600004", "theme": "AI", "start_date": as_of + pd.Timedelta(days=1), "end_date": pd.NaT},
    ])
    bars = bars.drop(columns=["theme"])
    _, target = _run(bars, dates, symbols, theme_map=theme_map, min_limitup_count=3)
    assert "600004" not in target.weights
    assert set(target.weights) == {"600001", "600002", "600003"}

    ended = theme_map.copy()
    ended.loc[ended["symbol"] == "600003", "end_date"] = as_of - pd.Timedelta(days=1)
    _, gone = _run(bars, dates, symbols, theme_map=ended, min_limitup_count=3)
    assert gone.weights == {}


def test_watchlist_future_rows_are_ignored_and_ungated_replay_is_explicit():
    symbols = ["600001", "600002"]
    bars, dates = _history(symbols, limit_symbols=[], n=4)
    as_of = dates[-1]
    watch = pd.DataFrame([
        {"trade_date": as_of, "symbol": "600001", "theme": "AI", "rank": 1},
        {"trade_date": as_of + pd.Timedelta(days=3), "symbol": "600002", "theme": "AI", "rank": 1},
    ])
    strat = HypeWatchStrategy(
        params=_params(csv_require_trigger=False, pool_size=3, min_limitup_count=1,
                       min_theme_members=1, min_theme_excess=None),
        watchlist=watch, benchmark=None)
    target = strat.generate_target(as_of, bars, symbols)
    assert list(target.weights) == ["600001"]
    assert any("只回放" in w for w in strat.warnings)


def test_csv_gate_drops_names_whose_theme_did_not_trigger():
    symbols = ["600001", "600002", "600003", "000001"]
    theme = {"600001": "AI", "600002": "AI", "600003": "AI", "000001": "BANK"}
    bars, dates = _history(symbols, limit_symbols=["600001", "600002", "600003"], theme_of=theme)
    as_of = dates[-1]
    watch = pd.DataFrame([
        {"trade_date": as_of, "symbol": "600001", "theme": "AI", "rank": 1},
        {"trade_date": as_of, "symbol": "000001", "theme": "BANK", "rank": 1},
    ])
    strat = HypeWatchStrategy(
        params=_params(input_mode="csv"), benchmark=_bench(dates), watchlist=watch)
    target = strat.generate_target(as_of, bars, symbols)
    assert set(target.weights) == {"600001"}
    bank = next(r for r in strat.signal_log if r["symbol"] == "000001")
    assert bank["reason"] == "theme_not_triggered"
    assert bank["selected"] is False


def test_rules_mode_refuses_a_watchlist_instead_of_ignoring_it():
    watch = pd.DataFrame([{"trade_date": "2024-01-02", "symbol": "600001", "theme": "AI"}])
    with pytest.raises(ValueError, match="rules"):
        HypeWatchStrategy(params=_params(input_mode="rules", min_theme_excess=None),
                          watchlist=watch, benchmark=None)


# ---------------------------------------------------------------- 观察窗口与断路

def test_forward_returns_use_trading_calendar_and_ignore_unselected_names():
    dates = pd.to_datetime(["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"])
    # 周五 → 下一交易日是周一，不是周六。
    rows = []
    prices = {
        "2024-01-05": 10.0,
        "2024-01-08": 11.0,
        "2024-01-09": 12.0,
        "2024-01-10": 13.0,
    }
    for d, px in prices.items():
        rows.append({
            "trade_date": pd.Timestamp(d), "symbol": "600001",
            "adj_close": px, "close": px,
        })
    bars = pd.DataFrame(rows)
    log = pd.DataFrame([
        {"trade_date": pd.Timestamp("2024-01-05"), "kind": "watch", "selected": True,
         "symbol": "600001", "theme": "AI"},
        {"trade_date": pd.Timestamp("2024-01-05"), "kind": "watch", "selected": False,
         "symbol": "600002", "theme": "AI"},
    ])
    obs = build_observations(log, bars, [1, 3, 5], dates)
    assert set(obs["symbol"]) == {"600001"}
    h1 = obs.loc[obs["horizon"] == 1].iloc[0]
    assert h1["observe_date"] == pd.Timestamp("2024-01-08")
    assert h1["status"] == "observed"
    assert h1["ret_close"] == pytest.approx(0.1)
    h3 = obs.loc[obs["horizon"] == 3].iloc[0]
    assert h3["status"] == "observed"
    assert h3["ret_close"] == pytest.approx(0.3)
    assert obs.loc[obs["horizon"] == 5, "status"].iloc[0] == "pending"
    assert "paper" in obs["label"].iloc[0]


def test_mutating_future_prices_changes_observations_not_the_signal():
    symbols = ["600001", "600002", "600003"]
    bars, dates = _history(symbols, n=8, limit_symbols=symbols)
    as_of = dates[3]
    # 把 as_of 当天收成涨停，后面几天维持原样。
    day = bars["trade_date"] == as_of
    bars.loc[day, "close"] = bars.loc[day, "limit_up"]
    bars.loc[day, "adj_close"] = bars.loc[day, "close"]
    strat = HypeWatchStrategy(params=_params(), benchmark=_bench(dates))
    weights = dict(strat.generate_target(as_of, bars, symbols).weights)
    obs = build_observations(strat.signal_log, bars, [1], dates)
    mutated = bars.copy()
    mutated.loc[mutated["trade_date"] > as_of, "adj_close"] *= 3.0
    strat2 = HypeWatchStrategy(params=_params(), benchmark=_bench(dates))
    assert dict(strat2.generate_target(as_of, mutated, symbols).weights) == weights
    obs2 = build_observations(strat2.signal_log, mutated, [1], dates)
    assert float(obs2["ret_close"].iloc[0]) != pytest.approx(float(obs["ret_close"].iloc[0]))


def test_circuit_breaker_trips_at_minus_8_percent_and_ignores_future_equity():
    dates = pd.bdate_range("2024-01-02", periods=20)
    symbols = ["600001", "600002", "600003"]
    bars, _ = _history(symbols, n=20, limit_symbols=symbols)
    # _history 的涨停只在最后一天。断路测试只看 observe_equity，信号日用最后一天。
    strat = HypeWatchStrategy(
        params=_params(circuit_enabled=True, circuit_lookback=20,
                       circuit_max_drawdown=-0.08, circuit_cooldown_days=3),
        benchmark=_bench(dates))
    peak = pd.Series(100.0, index=dates)
    almost = peak.copy()
    almost.iloc[-1] = 92.1
    strat.observe_equity(dates[-1], almost)
    held = strat.generate_target(dates[-1], bars, symbols)
    assert held.weights, "92.1/100 约 -7.9%，还没到 -8%"

    strat2 = HypeWatchStrategy(
        params=_params(circuit_enabled=True, circuit_lookback=20,
                       circuit_max_drawdown=-0.08, circuit_cooldown_days=2),
        benchmark=_bench(dates))
    hit = peak.copy()
    hit.iloc[-1] = 92.0
    # 明天再跌一段，不应该提前把今天打成断路。
    hit_with_future = pd.concat([
        hit, pd.Series({dates[-1] + pd.Timedelta(days=1): 50.0})])
    strat2.observe_equity(dates[-1], hit_with_future)
    flat = strat2.generate_target(dates[-1], bars, symbols)
    assert flat.weights == {}
    assert any(r["reason"] == "circuit_breaker" for r in strat2.signal_log)

    # 下一天规则仍然会触发（涨停还在），但冷静期没走完，权益创新高也继续空仓。
    nxt = dates[-1] + pd.offsets.BDay(1)
    extra = bars.loc[bars["trade_date"] == dates[-1]].copy()
    pre = extra.set_index("symbol")["close"]
    extra["pre_close"] = extra["symbol"].map(pre)
    extra["open"] = extra["pre_close"]
    extra["limit_up"] = (extra["pre_close"] * 1.10).round(2)
    extra["limit_down"] = (extra["pre_close"] * 0.90).round(2)
    extra["close"] = extra["limit_up"]
    extra["adj_close"] = extra["close"]
    extra["high"] = extra["close"]
    extra["low"] = extra["open"]
    extra["trade_date"] = nxt
    bars_ext = pd.concat([bars, extra], ignore_index=True)
    bench_ext = pd.concat([_bench(dates), pd.Series({nxt: 1000.0})])
    strat2._benchmark = bench_ext.sort_index()
    recovered = pd.Series(200.0, index=list(dates) + [nxt])
    strat2.observe_equity(nxt, recovered)
    still = strat2.generate_target(nxt, bars_ext, symbols)
    assert still.weights == {}
    assert any(r["reason"] == "circuit_cooldown" for r in strat2.signal_log)


def test_old_peak_outside_the_lookback_does_not_trip():
    dates = pd.bdate_range("2024-01-02", periods=25)
    symbols = ["600001", "600002", "600003"]
    bars, _ = _history(symbols, n=25, limit_symbols=symbols)
    eq = pd.Series(100.0, index=dates)
    eq.iloc[0] = 200.0          # 25 天前的高点，20 日窗口看不到
    eq.iloc[-1] = 95.0          # 窗口内回撤 -5%
    strat = HypeWatchStrategy(
        params=_params(circuit_enabled=True, circuit_lookback=20,
                       circuit_max_drawdown=-0.08, circuit_cooldown_days=2),
        benchmark=_bench(dates))
    strat.observe_equity(dates[-1], eq)
    target = strat.generate_target(dates[-1], bars, symbols)
    assert target.weights
    assert strat._circuit_reason != "circuit_breaker"


def test_drawdown_unit_must_be_a_fraction():
    with pytest.raises(ValueError, match="-0.08"):
        HypeWatchParams(circuit_max_drawdown=-8)


def test_paper_only_false_and_string_false_are_rejected():
    with pytest.raises(ValueError, match="paper_only"):
        HypeWatchParams(paper_only=False)
    with pytest.raises(TypeError, match="paper_only"):
        HypeWatchParams.from_mapping({"paper_only": "false"})


# ---------------------------------------------------------------- 配置、CSV、实盘隔离

def test_settings_yaml_keeps_global_costs_and_sets_ten_bps_one_way():
    raw = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert raw["costs"]["commission_rate"] == 0.00025
    assert raw["costs"]["slippage_rate"] == 0.0005
    assert raw["backtest"]["rebalance_frequency"] == "M"
    assert raw["backtest"]["execution_price"] == "open"
    params = HypeWatchParams.from_mapping(raw["hype_watch"])
    assert params.paper_only is True
    assert params.one_way_friction() == pytest.approx(0.001)
    assert params.pool_size == 3
    assert params.max_themes == 3
    assert params.min_limitup_count == 3
    assert params.min_theme_excess == pytest.approx(0.01)
    assert params.circuit_lookback == 20
    assert params.circuit_max_drawdown == pytest.approx(-0.08)
    assert params.circuit_cooldown_days == 5
    assert params.observe_horizons == (1, 3, 5)
    assert params.execution_price == "open"
    assert CostModel().slippage_rate == 0.0005
    with pytest.raises(ValueError, match="未知键"):
        HypeWatchParams.from_mapping({**raw["hype_watch"], "optimize_for_return": True})


def test_runtime_overrides_refuse_max_positions_clip():
    params = _params()
    over = runtime_overrides(params)
    assert over["max_positions"] == 3
    assert over["max_weight_per_stock"] == 1.0
    assert over["execution_price"] == "open"
    assert over["rebalance_frequency"] == "D"
    with pytest.raises(ValueError, match="pool_size"):
        runtime_overrides(params, max_positions=1)


def test_csv_loaders_reject_bad_tables(tmp_path):
    bad = tmp_path / "watch.csv"
    bad.write_text("trade_date,symbol\n2024-01-02,600001\n", encoding="utf-8")
    with pytest.raises(ValueError, match="theme"):
        load_watchlist_csv(bad)
    num = tmp_path / "num.csv"
    num.write_text("trade_date,symbol,theme\n2024-01-02,600001.0,AI\n", encoding="utf-8")
    with pytest.raises(ValueError, match="字符串"):
        load_watchlist_csv(num)
    dup = tmp_path / "dup.csv"
    dup.write_text(
        "trade_date,symbol,theme\n2024-01-02,600001,AI\n2024-01-02,600001,AI\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_watchlist_csv(dup)
    themes = tmp_path / "themes.csv"
    themes.write_text("symbol,theme\n600001,AI\n", encoding="utf-8")
    with pytest.raises(ValueError, match="start_date"):
        load_theme_map_csv(themes)
    with pytest.raises(ValueError, match="start_date"):
        normalize_theme_map(pd.DataFrame(
            [{"symbol": "600001", "theme": "AI", "start_date": None}]))
    ok = tmp_path / "ok.csv"
    ok.write_text(
        "trade_date,symbol,theme,rank\n2024-01-02,600001,AI,1\n", encoding="utf-8")
    loaded = load_watchlist_csv(ok)
    assert loaded["symbol"].tolist() == ["600001"]
    assert normalize_watchlist(loaded)["symbol"].tolist() == ["600001"]


def test_live_entrypoints_do_not_reference_hype_watch():
    paths = [
        ROOT / "src/a_share_quant/live/signal.py",
        ROOT / "src/a_share_quant/live/reconcile.py",
        ROOT / "scripts/live_signal.py",
        ROOT / "scripts/live_auto.py",
        ROOT / "scripts/reconcile.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "hype_watch" not in text, path
        assert "HypeWatch" not in text, path


# ---------------------------------------------------------------- 引擎：T+1、收盘价、断路

def _engine_bars(dates, symbols, signal_day, exec_day, *, crash=None, themes=None,
                 limit_on=None):
    themes = themes or {s: "AI" for s in symbols}
    limit_on = limit_on or {}
    px = {s: 10.0 for s in symbols}
    rows = []
    for d in dates:
        for s in symbols:
            pre = px[s]
            lu = round(pre * 1.10, 2)
            ld = round(pre * 0.90, 2)
            if d in limit_on and s in limit_on[d]:
                close = lu
            elif crash and d == crash[0] and s in crash[1]:
                close = ld
            else:
                close = round(pre * 1.001, 4)
            open_px = pre
            if d == exec_day and s == symbols[0]:
                open_px = round(pre, 4)
            rows.append({
                "trade_date": pd.Timestamp(d), "symbol": s,
                "open": float(open_px),
                "high": float(max(open_px, close)),
                "low": float(min(open_px, close)),
                "close": float(close),
                "volume": 1_000_000.0,
                "amount": 1e8,
                "adj_close": float(close),
                "pre_close": float(pre),
                "limit_up": float(lu),
                "limit_down": float(ld),
                "limit_exempt": False,
                "is_st": False,
                "is_suspended": False,
                "theme": themes[s],
            })
            px[s] = close
    return pd.DataFrame(rows)


def _run_engine(bars, dates, symbols, strategy, *, execution_price="open", cash_buffer=0.0):
    provider = make_provider(dates, symbols, bars=bars)
    engine = BacktestEngine(
        provider=provider,
        strategy=strategy,
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=5, max_weight_per_stock=1.0),
        cost_model=CostModel(commission_rate=0.0, commission_min=0.0,
                              stamp_duty_rate=0.0, slippage_rate=0.0),
        config=BacktestConfig(
            start_date=str(dates[0].date()),
            end_date=str(dates[-1].date()),
            rebalance_frequency="D",
            initial_capital=1_000_000.0,
            execution_price=execution_price,
            cash_buffer=cash_buffer),
        enforce_limit=True)
    return engine.run()


def test_signal_fills_on_the_next_session_at_the_open():
    dates = pd.bdate_range("2024-01-02", periods=6)
    symbols = ["600001", "600002", "600003"]
    signal_day, exec_day = dates[3], dates[4]
    bars = _engine_bars(dates, symbols, signal_day, exec_day,
                        limit_on={signal_day: set(symbols)})
    # 成交日开盘故意偏离收盘，用来确认撮合列是 open。涨停价抬高，避免这天又被当成触发日。
    mask = bars["trade_date"] == exec_day
    bars.loc[mask, "open"] = 10.5
    bars.loc[mask, "close"] = 12.0
    bars.loc[mask, "high"] = 12.0
    bars.loc[mask, "low"] = 10.5
    bars.loc[mask, "limit_up"] = 13.0
    bars.loc[mask, "limit_down"] = 9.0
    strat = HypeWatchStrategy(params=_params(circuit_enabled=False), benchmark=_bench(dates))
    result = _run_engine(bars, dates, symbols, strat, execution_price="open")
    filled = result.trades[(result.trades["shares"] > 0) & (result.trades["note"] == "filled")
                           & (result.trades["direction"] == "buy")]
    assert not filled.empty
    assert set(pd.to_datetime(filled["trade_date"])) == {pd.Timestamp(exec_day)}
    assert set(filled["symbol"]) == set(symbols)
    assert set(filled["price"]) == {10.5}
    assert signal_day not in set(pd.to_datetime(filled["trade_date"]))
    selected = [r for r in strat.signal_log
                if r["selected"] and pd.Timestamp(r["trade_date"]) == signal_day]
    assert {r["symbol"] for r in selected} == set(symbols)


def test_close_execution_uses_the_close_and_a_sealed_close_blocks_the_buy():
    dates = pd.bdate_range("2024-02-01", periods=5)
    symbols = ["600001", "600002", "600003"]
    signal_day, exec_day = dates[2], dates[3]
    bars = _engine_bars(dates, symbols, signal_day, exec_day,
                        limit_on={signal_day: set(symbols)})
    mask = bars["trade_date"] == exec_day
    bars.loc[mask, "open"] = 10.5
    bars.loc[mask, "close"] = 12.25
    bars.loc[mask, "high"] = 12.25
    bars.loc[mask, "low"] = 10.5
    bars.loc[mask, "limit_up"] = 13.0
    bars.loc[mask, "limit_down"] = 9.0
    strat = HypeWatchStrategy(
        params=_params(circuit_enabled=False, execution_price="close"),
        benchmark=_bench(dates))
    result = _run_engine(bars, dates, symbols, strat, execution_price="close")
    filled = result.trades[(result.trades["shares"] > 0) & (result.trades["note"] == "filled")
                           & (result.trades["direction"] == "buy")]
    assert set(pd.to_datetime(filled["trade_date"])) == {pd.Timestamp(exec_day)}
    assert set(filled["price"]) == {12.25}

    sealed = bars.copy()
    sealed.loc[mask, "close"] = 13.0
    sealed.loc[mask, "limit_up"] = 13.0
    sealed.loc[mask, "high"] = 13.0
    sealed.loc[mask, "low"] = 10.5
    sealed.loc[mask, "open"] = 10.5
    strat2 = HypeWatchStrategy(params=_params(circuit_enabled=False), benchmark=_bench(dates))
    blocked = _run_engine(sealed, dates, symbols, strat2, execution_price="close")
    buys = blocked.trades[(blocked.trades["trade_date"] == exec_day)
                          & (blocked.trades["direction"] == "buy")]
    assert not buys.empty
    assert set(buys["note"]) == {"blocked:limit_up"}
    assert int(buys["shares"].sum()) == 0


def test_circuit_breaker_flattens_through_the_engine_and_blocks_the_next_theme():
    dates = pd.bdate_range("2024-03-01", periods=8)
    symbols = ["600001", "600002"]
    themes = {"600001": "AI", "600002": "BETA"}
    first, crash_day, second = dates[3], dates[4], dates[5]
    bars = _engine_bars(
        dates, symbols, first, crash_day, themes=themes,
        limit_on={first: {"600001"}, second: {"600002"}},
        crash=(crash_day, {"600001"}))
    strat = HypeWatchStrategy(
        params=_params(
            circuit_enabled=True, circuit_lookback=4, circuit_max_drawdown=-0.08,
            circuit_cooldown_days=3, pool_size=1, max_themes=1,
            min_limitup_count=1, min_theme_members=1, min_theme_excess=0.01),
        benchmark=_bench(dates))
    result = _run_engine(bars, dates, symbols, strat, cash_buffer=0.0)
    filled = result.trades[(result.trades["shares"] > 0) & (result.trades["note"] == "filled")]
    bought = set(filled.loc[filled["direction"] == "buy", "symbol"])
    assert "600001" in bought
    assert "600002" not in bought
    assert any(r["reason"] == "circuit_breaker" for r in strat.signal_log)
    summary = paper_summary(result, strat.signal_log, strat.params)
    assert summary["label"] == "paper"
    assert "不是买卖建议" in summary["disclaimer"]
    assert summary["research_claim"].startswith("未验证")
    assert summary["n_rebalances"] >= 1
    assert "final_equity" in summary and "maximum_drawdown" in summary
    assert "total_costs" in summary
