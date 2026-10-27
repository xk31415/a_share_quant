"""观察池策略实盘信号桥接：把 hype_watch 的目标权重转成委托单。

为什么不改 hype_watch.py
------------------------
`hype_watch.py` 的 `paper_only` 是硬锁（必须为 True）。**这个锁不该被拆**。
本脚本是独立的桥接层：只调 `generate_target()` 拿权重，然后走 `diff_orders()`
出委托单。这不是买卖建议，也不表示规则已通过研究判定。

题材来源
--------
hype_watch 的纯规则路径需要题材成员。本脚本按优先级取：
1. `--themes`（CSV，symbol,theme,start_date[,end_date]）—— 正确做法
2. `instruments.industry` —— 静态分类代理。只允许 as_of 等于数据最后一个
   交易日（当天仿真）。更早的日期直接拒绝：用现在的行业回放历史是前视。

股票池与 live_signal.py 同一套 universe.yaml：板块白名单、上市天数、
ST/停牌/退市。指数代码 000300 始终排除。000852 是基准也是石化机械，
不因为当基准就被剔出股票池。

断路器
------
hype_watch 的 `observe_equity` 由引擎调用。实盘没有引擎，这里维护
`live/equity_history.csv`（账户估值：现金 + 持仓市值），每次出信号前喂给策略。
`--dry-run` 不写这个文件，也不写 orders.csv。

用法
----
    python scripts/live_signal_hype.py --data-dir data/processed_2025_2026 \
        --cash 1000000 --holdings live/holdings.csv \
        --as-of 2026-10-24 --out-dir live

输出与 live_signal.py 一致：live/<as_of>/orders.csv
没传 --position 时读 config/live.yaml 的 position，不默认满仓。
数据距今超过 15 天拒绝出单（与 live_signal.py 相同）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from a_share_quant.data.loader import LocalFileProvider  # noqa: E402
from a_share_quant.live.signal import (  # noqa: E402
    DEFAULT_LOT_SIZE,
    DEFAULT_MIN_TRADE_VALUE,
    diff_orders,
)
from a_share_quant.strategies.hype_watch import (  # noqa: E402
    HypeWatchParams,
    HypeWatchStrategy,
    PAPER_DISCLAIMER,
    load_theme_map_csv,
)
from a_share_quant.universe.builder import UniverseBuilder, UniverseRules  # noqa: E402

EQ_HISTORY = "equity_history.csv"
# 与 scripts/live_signal.py 的 MAX_STALE_DAYS 相同；测试锁住这两个常数。
MAX_STALE_DAYS = 15
MIN_UNIVERSE = 200


def _load_holdings(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"持仓文件不存在：{p}")
    df = pd.read_csv(p, dtype={"symbol": str})
    missing = {"symbol", "shares"} - set(df.columns)
    if missing:
        raise SystemExit(f"持仓文件缺列：{sorted(missing)}（需要 symbol,shares）")
    out: dict[str, int] = {}
    for sym, sh in zip(df["symbol"], pd.to_numeric(df["shares"], errors="coerce")):
        if pd.isna(sh) or int(sh) <= 0:
            continue
        out[str(sym).strip()] = int(sh)
    return out


def _resolve_as_of(cal, requested: str | None) -> pd.Timestamp:
    dates = cal.dates
    if requested:
        ts = pd.Timestamp(requested).normalize()
        if ts in cal:
            return ts
        earlier = dates[dates <= ts]
        if len(earlier) == 0:
            raise SystemExit(f"--as-of {requested} 早于数据起点 {dates[0].date()}")
        print(f"[warn] {requested} 不是交易日，改用 {earlier[-1].date()}", flush=True)
        return earlier[-1]
    return dates[-1]


def _latest_prices(bars: pd.DataFrame, symbols: set[str],
                   as_of: pd.Timestamp) -> dict[str, float]:
    """as_of 当日收盘价；当日没有的，取之前最近一个。停牌股给最后价。"""
    b = bars.loc[(bars["trade_date"] <= as_of) & (bars["symbol"].isin(symbols))]
    if b.empty:
        return {}
    b = b.sort_values("trade_date")
    last = b.groupby("symbol").last()
    return {str(s): float(r["close"]) for s, r in last.iterrows()}


def _load_bars(data_dir: Path) -> pd.DataFrame:
    p = data_dir / "bars.parquet"
    if not p.exists():
        raise SystemExit(f"缺 {p}")
    b = pd.read_parquet(p)
    b["trade_date"] = pd.to_datetime(b["trade_date"]).dt.normalize()
    b["symbol"] = b["symbol"].astype(str)
    return b


def _load_instruments(data_dir: Path) -> pd.DataFrame:
    p = data_dir / "instruments.parquet"
    if not p.exists():
        raise SystemExit(f"缺 {p}")
    inst = pd.read_parquet(p)
    inst["symbol"] = inst["symbol"].astype(str)
    return inst


def _load_benchmark(data_dir: Path, code: str) -> pd.Series:
    p = data_dir / "benchmark.csv"
    if not p.exists():
        raise SystemExit(f"缺 {p}。hype_watch 的超额门槛需要基准序列。")
    df = pd.read_csv(p, dtype={"symbol": str})
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    rows = df.loc[df["symbol"] == code]
    if rows.empty:
        raise SystemExit(f"benchmark.csv 里没有 {code}")
    s = rows.set_index("trade_date")["close"].sort_index()
    s.index = s.index.normalize()
    s = s[~s.index.duplicated(keep="last")].dropna()
    return s


def _build_theme_map_from_instruments(inst: pd.DataFrame) -> pd.DataFrame:
    """用 instruments.industry 当题材代理。

    实盘只看当天，静态分类没有前视问题。回测不要用这个路径。
    """
    if "industry" not in inst.columns:
        raise SystemExit(
            "instruments 没有 industry 列，也未提供 --themes。"
            "纯规则路径必须有题材成员——请提供 themes.csv（symbol,theme,start_date）。")
    rows = []
    for r in inst.itertuples(index=False):
        ind = getattr(r, "industry", None)
        if pd.isna(ind) or not str(ind).strip():
            continue
        ld = getattr(r, "list_date", None)
        dd = getattr(r, "delist_date", None)
        rows.append({
            "symbol": str(r.symbol),
            "theme": str(ind).strip(),
            "start_date": ld if pd.notna(ld) else pd.Timestamp("2010-01-01"),
            "end_date": dd if dd is not None and pd.notna(dd) else pd.NaT,
        })
    df = pd.DataFrame(rows)
    print(f"[info] 题材代理：instruments.industry，{df['theme'].nunique()} 个行业 / "
          f"{len(df)} 只。静态分类有前视（实盘无碍，回测勿用）。")
    return df


def _load_equity_history(path: Path) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype="float64")
    df = pd.read_csv(path)
    if "trade_date" not in df.columns or "equity" not in df.columns:
        return pd.Series(dtype="float64")
    s = pd.Series(
        df["equity"].astype(float).values,
        index=pd.to_datetime(df["trade_date"]).dt.normalize(),
        name="equity")
    return s.sort_index()


def live_excluded_symbols(excluded) -> list[str]:
    """与回测入口同一口径：universe.yaml 的剔除名单，并始终去掉 000300。

    基准代码 000852 同时是石化机械。指数价格在 benchmark.csv，
    不能因为当了基准就把这只股票剔出股票池。
    """
    out = [str(s) for s in (excluded or [])]
    if "000300" not in set(out):
        out.append("000300")
    return out


def assert_data_fresh(as_of, today=None, max_stale: int = MAX_STALE_DAYS) -> None:
    """数据过期就拒绝出单。与 live_signal.py 同一阈值。"""
    today_ts = (pd.Timestamp.today().normalize() if today is None
                else pd.Timestamp(today).normalize())
    stale_days = (today_ts - pd.Timestamp(as_of).normalize()).days
    if stale_days > max_stale:
        raise SystemExit(
            f"数据最后交易日 {pd.Timestamp(as_of).date()}，距今 {stale_days} 天 "
            f"（阈值 {max_stale}）。先用 update_dataset 更新数据，"
            f"不要用过期行情出单。")


def resolve_position(cli, configured) -> float:
    """命令行优先；否则用 live.yaml。两边都没有就拒绝，不默认满仓。"""
    if cli is not None:
        value = float(cli)
    elif configured is not None:
        value = float(configured)
    else:
        raise SystemExit(
            "请显式传 --position，或在 config/live.yaml 写 position。"
            "不会默认满仓。")
    if not 0.0 < value <= 1.0:
        raise SystemExit(f"position 必须在 (0, 1]，收到 {value}")
    return value


def account_capital(cash, capital_arg, held_value: float, fallback: float) -> float:
    """总资产。给了现金就用现金 + 持仓市值；否则用传入的总资产，不再把市值加一遍。"""
    if cash is not None:
        return float(cash) + float(held_value)
    if capital_arg is not None:
        return float(capital_arg)
    return float(fallback)


def require_prices(symbols, prices: dict, what: str) -> None:
    """缺价拒绝出单。静默跳过会把持仓估值算少，或把选中的票从委托里丢掉。"""
    missing = []
    for sym in symbols:
        px = prices.get(sym)
        if px is None or not (float(px) > 0.0):
            missing.append(str(sym))
    if missing:
        raise SystemExit(
            f"{what} {missing[:8]} 没有正的收盘价，拒绝出单。"
            f"请确认行情覆盖到这些代码。")


def refuse_static_theme_replay(as_of, last_date) -> None:
    """静态行业只能用于数据里的最后一个交易日。更早的 as_of 是前视。"""
    if pd.Timestamp(as_of).normalize() < pd.Timestamp(last_date).normalize():
        raise SystemExit(
            "as_of 早于数据最后一个交易日，不能用 instruments.industry 当题材。"
            "静态分类回放历史是前视。请传 --themes themes.csv（含 start_date）。")


def _append_equity(path: Path, ts: pd.Timestamp, equity: float) -> None:
    rows = _load_equity_history(path)
    # 追加或覆盖当日
    rows.loc[ts] = equity
    rows = rows.sort_index()
    out = pd.DataFrame({"trade_date": rows.index, "equity": rows.values})
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, encoding="utf-8")


def write_live_bundle(*, dry_run: bool, date_dir: Path, orders_df: pd.DataFrame,
                      log_df: pd.DataFrame, summary: dict, eq_path: Path,
                      equity_ts, equity: float) -> bool:
    """写委托、信号日志和断路器用的权益。dry-run 什么都不写。"""
    if dry_run:
        return False
    date_dir.mkdir(parents=True, exist_ok=True)
    orders_df.to_csv(date_dir / "orders.csv", index=False, encoding="utf-8")
    log_df.to_csv(date_dir / "signal_log.csv", index=False, encoding="utf-8")
    (date_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    _append_equity(eq_path, pd.Timestamp(equity_ts), float(equity))
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="观察池策略实盘信号桥接")
    ap.add_argument("--data-dir", default="data/processed_2025_2026")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--universe-config", default="config/universe.yaml")
    ap.add_argument("--live-config", default="config/live.yaml",
                    help="没传 --position 时读这里的 position")
    ap.add_argument("--themes", default=None,
                    help="题材成员 CSV（symbol,theme,start_date[,end_date]）。"
                         "不给则仅在数据最后一个交易日用 instruments.industry 当代理")
    ap.add_argument("--holdings", default=None, help="当前持仓 CSV（symbol,shares）")
    ap.add_argument("--cash", type=float, default=None, help="账户可用现金")
    ap.add_argument("--capital", type=float, default=None, help="账户总资产")
    ap.add_argument("--as-of", default=None, help="信号日（默认数据最后一个交易日）")
    ap.add_argument("--out-dir", default="live")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--position", type=float, default=None,
                    help="目标仓位 (0, 1]。缺省读 live.yaml，不默认 1")
    ap.add_argument("--ignore-quarter", action="store_true",
                    help="hype_watch 是日频，此参数无效（兼容 live_auto 调用）")
    args = ap.parse_args()

    root = ROOT
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    if not data_dir.exists():
        raise SystemExit(f"数据目录不存在：{data_dir}")

    # ---- 数据 ----
    provider = LocalFileProvider(data_dir)
    cal = provider.get_calendar()
    as_of = _resolve_as_of(cal, args.as_of)
    assert_data_fresh(as_of)
    bars = _load_bars(data_dir)
    inst = _load_instruments(data_dir)

    # ---- 参数 ----
    import yaml
    settings = yaml.safe_load(
        (root / args.config).read_text(encoding="utf-8")) or {}
    live_cfg = {}
    live_path = root / args.live_config
    if live_path.exists():
        live_cfg = yaml.safe_load(live_path.read_text(encoding="utf-8")) or {}
    hw_cfg = settings.get("hype_watch") or {}
    params = HypeWatchParams.from_mapping(hw_cfg)
    position = resolve_position(args.position, live_cfg.get("position"))
    market = settings.get("market_rules") or {}
    lot_size = int(market.get("lot_size", 100))
    min_trade = DEFAULT_MIN_TRADE_VALUE
    fallback_capital = float((settings.get("backtest") or {}).get(
        "initial_capital", 1_000_000))

    # ---- 股票池（与回测同一套规则，含科创板/北交所排除）----
    uni_path = root / args.universe_config
    rules = (UniverseRules.from_yaml(uni_path) if uni_path.exists()
             else UniverseRules())
    builder = UniverseBuilder(provider, rules)
    universe = builder.get_universe(as_of)
    for msg in list(builder.warnings) + list(builder.finalize_warnings()):
        print(f"[warn] {msg}", flush=True)
    if len(universe) < MIN_UNIVERSE:
        raise SystemExit(
            f"股票池只有 {len(universe)} 只，明显异常，拒绝出信号")
    print(f"股票池 {len(universe)} 只", flush=True)

    # ---- 题材 ----
    if args.themes:
        tp = Path(args.themes)
        if not tp.is_absolute():
            tp = root / tp
        theme_map = load_theme_map_csv(tp)
        print(f"[info] 题材表：{tp}（{theme_map['theme'].nunique()} 个题材）")
    else:
        refuse_static_theme_replay(as_of, cal.dates[-1])
        theme_map = _build_theme_map_from_instruments(inst)

    # ---- 基准 ----
    bench_code = str(params.benchmark or "000852")
    benchmark = _load_benchmark(data_dir, bench_code)

    # ---- 排除指数代码，保留与指数撞码的个股（000852 = 石化机械）----
    excluded = live_excluded_symbols(rules.excluded_symbols)

    # ---- 策略 ----
    strategy = HypeWatchStrategy(
        params=params, theme_map=theme_map,
        benchmark=benchmark, excluded_symbols=excluded)

    # ---- 断路器：喂历史权益（只读，dry-run 也不写）----
    out_dir = root / args.out_dir
    eq_path = out_dir / EQ_HISTORY
    equity_hist = _load_equity_history(eq_path)
    strategy.observe_equity(as_of, equity_hist)

    # ---- 出信号 ----
    target = strategy.generate_target(as_of, bars, universe)
    weights = target.weights   # {symbol: weight}

    # ---- 价格 ----
    holdings = _load_holdings(args.holdings) if args.holdings else {}
    need_prices = set(weights) | set(holdings)
    prices = _latest_prices(bars, need_prices, as_of)
    require_prices(holdings, prices, "持仓")
    require_prices(weights, prices, "观察池")

    # ---- 资金：现金 + 持仓市值。没给现金时用总资产，不再把市值加一遍 ----
    held_value = sum(sh * prices[s] for s, sh in holdings.items())
    capital = account_capital(
        args.cash, args.capital, held_value, fallback_capital)
    if args.cash is None and holdings:
        print(f"[warn] 未传 --cash，用总资产 {capital:,.0f} 出单；"
              f"持仓市值已经含在总资产里，不会再加一遍。"
              f"更可靠的是 --cash。", flush=True)

    # ---- 权重 → 目标股数 ----
    target_shares: dict[str, int] = {}
    for sym, w in weights.items():
        px = prices[sym]
        amount = float(w) * capital * position
        shares = int(amount / px / lot_size) * lot_size
        if shares > 0:
            target_shares[sym] = shares

    # ---- 出委托 ----
    orders = diff_orders(target_shares, holdings, prices,
                         lot_size=lot_size, min_trade_value=min_trade)

    # 涨跌停不在这里拦：本策略的候选恰恰是涨停股，用信号日收盘价拦等于永远买不进
    # （2026-10-27 实测 3/3 被 blocked:limit_up）。判定在**下单时**做——
    # gm_bridge.place-orders 的执行日闸门，口径与 backtest/execution.py 一致。
    # orders.csv 是策略意图；实际发送的见 placement.csv。

    orders_df = pd.DataFrame(orders,
                             columns=["symbol", "side", "shares", "price",
                                      "amount", "note"]) if orders else pd.DataFrame(
        columns=["symbol", "side", "shares", "price", "amount", "note"])
    log_df = pd.DataFrame(strategy.signal_log)
    summary = {
        "as_of": as_of.isoformat(),
        "strategy": "hype_watch",
        "mode": strategy._mode,
        "pool_size": params.pool_size,
        "position": position,
        "weights": {k: round(v, 4) for k, v in weights.items()},
        "target_shares": target_shares,
        "capital": round(capital, 2),
        "n_orders": len(orders),
        "circuit_flat": strategy._circuit_flat,
        "circuit_reason": strategy._circuit_reason,
        "warnings": strategy.warnings,
        "disclaimer": PAPER_DISCLAIMER,
    }

    # ---- 打印 ----
    print(f"\n策略 hype_watch  信号日 {as_of.date()}  模式 {strategy._mode}  "
          f"资金 {capital:,.0f}  仓位 {position:.0%}")
    if strategy._circuit_flat:
        print(f"  **断路器触发**（{strategy._circuit_reason}），目标空仓")
    if weights:
        print(f"  观察池 {len(weights)} 只:")
        for sym, w in sorted(weights.items(), key=lambda x: -x[1]):
            sh = target_shares.get(sym, 0)
            px = prices.get(sym, 0)
            print(f"    {sym}  权重 {w:.3f}  {sh} 股 @ {px:.2f}")
    else:
        print("  今日无触发（空仓）")
    for w in strategy.warnings:
        print(f"  [warn] {w}")

    # 断路器记的是出信号时的账户估值（现金 + 持仓市值），不是目标仓位的市值。
    wrote = write_live_bundle(
        dry_run=bool(args.dry_run),
        date_dir=out_dir / as_of.strftime("%Y-%m-%d"),
        orders_df=orders_df,
        log_df=log_df,
        summary=summary,
        eq_path=eq_path,
        equity_ts=as_of,
        equity=capital)
    if not wrote:
        print("\n--dry-run：未写文件。")
        return
    print(f"\n已写入 {out_dir / as_of.strftime('%Y-%m-%d')}")
    print(f"委托 {len(orders)} 笔（orders.csv）")


if __name__ == "__main__":
    main()
