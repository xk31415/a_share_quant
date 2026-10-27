"""观察池策略实盘信号桥接：把 hype_watch 的目标权重转成委托单。

为什么不改 hype_watch.py
------------------------
`hype_watch.py` 的 `paper_only` 是硬锁（必须为 True），声明"不接入 live_signal
/ live_auto"。**这个锁不该被拆**——它是 PR 的安全声明。本脚本是独立的桥接层：
只调 `generate_target()` 拿权重，然后走 `diff_orders()` 出委托单，不碰策略内部。

题材来源
--------
hype_watch 的纯规则路径需要题材成员。本脚本按优先级取：
1. `--themes`（CSV，symbol,theme,start_date[,end_date]）—— 正确做法
2. `instruments.industry` —— 静态分类代理，**有前视**（README 限制第 5/30 条），
   实盘用没问题（只看当天），回测不要用

断路器
------
hype_watch 的 `observe_equity` 由引擎调用。实盘没有引擎，这里维护
`live/equity_history.csv`，每次出信号前喂给策略。

用法
----
    python scripts/live_signal_hype.py --data-dir data/processed_2025_2026 \
        --cash 1000000 --holdings live/holdings.csv \
        --as-of 2026-10-24 --out-dir live

输出与 live_signal.py 一致：live/<as_of>/orders.csv
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
    runtime_overrides,
)

EQ_HISTORY = "equity_history.csv"


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


def _append_equity(path: Path, ts: pd.Timestamp, equity: float) -> None:
    rows = _load_equity_history(path)
    # 追加或覆盖当日
    rows.loc[ts] = equity
    rows = rows.sort_index()
    out = pd.DataFrame({"trade_date": rows.index, "equity": rows.values})
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="观察池策略实盘信号桥接")
    ap.add_argument("--data-dir", default="data/processed_2025_2026")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--themes", default=None,
                    help="题材成员 CSV（symbol,theme,start_date[,end_date]）。"
                         "不给则用 instruments.industry 当代理")
    ap.add_argument("--holdings", default=None, help="当前持仓 CSV（symbol,shares）")
    ap.add_argument("--cash", type=float, default=None, help="账户可用现金")
    ap.add_argument("--capital", type=float, default=None, help="账户总资产")
    ap.add_argument("--as-of", default=None, help="信号日（默认数据最后一个交易日）")
    ap.add_argument("--out-dir", default="live")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--position", type=float, default=1.0)
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
    bars = _load_bars(data_dir)
    inst = _load_instruments(data_dir)

    # ---- 参数 ----
    import yaml
    settings = yaml.safe_load(
        (root / args.config).read_text(encoding="utf-8")) or {}
    hw_cfg = settings.get("hype_watch") or {}
    params = HypeWatchParams.from_mapping(hw_cfg)
    overrides = runtime_overrides(params)   # hype_watch 的 pool_size 等
    lot_size = 100
    min_trade = DEFAULT_MIN_TRADE_VALUE

    # ---- 题材 ----
    theme_map = None
    if args.themes:
        tp = Path(args.themes)
        if not tp.is_absolute():
            tp = root / tp
        theme_map = load_theme_map_csv(tp)
        print(f"[info] 题材表：{tp}（{theme_map['theme'].nunique()} 个题材）")
    else:
        theme_map = _build_theme_map_from_instruments(inst)

    # ---- 基准 ----
    bench_code = str(params.benchmark or "000300")
    benchmark = _load_benchmark(data_dir, bench_code)

    # ---- 排除指数 ----
    excluded = ["000852"]

    # ---- 策略 ----
    strategy = HypeWatchStrategy(
        params=params, theme_map=theme_map,
        benchmark=benchmark, excluded_symbols=excluded)

    # ---- 断路器：喂历史权益 ----
    eq_path = root / args.out_dir / EQ_HISTORY
    equity_hist = _load_equity_history(eq_path)
    strategy.observe_equity(as_of, equity_hist)

    # ---- 宇宙 ----
    universe = inst["symbol"].astype(str).tolist()

    # ---- 出信号 ----
    target = strategy.generate_target(as_of, bars, universe)
    weights = target.weights   # {symbol: weight}

    # ---- 价格 ----
    holdings = _load_holdings(args.holdings) if args.holdings else {}
    need_prices = set(weights) | set(holdings)
    prices = _latest_prices(bars, need_prices, as_of)

    # ---- 资金 ----
    held_value = sum(
        sh * prices.get(s, 0) for s, sh in holdings.items() if s in prices)
    if args.cash is not None:
        capital = float(args.cash) + held_value
    elif args.capital is not None:
        capital = float(args.capital)
    else:
        capital = 1_000_000.0 + held_value

    # ---- 权重 → 目标股数 ----
    target_shares: dict[str, int] = {}
    for sym, w in weights.items():
        px = prices.get(sym)
        if px is None or px <= 0:
            continue
        amount = float(w) * capital * float(args.position)
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

    # ---- 输出 ----
    out_dir = root / args.out_dir
    date_dir = out_dir / as_of.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)

    # orders.csv（与 live_signal.py 同格式）
    orders_df = pd.DataFrame(orders,
                             columns=["symbol", "side", "shares", "price",
                                      "amount", "note"]) if orders else pd.DataFrame(
        columns=["symbol", "side", "shares", "price", "amount", "note"])
    orders_df.to_csv(date_dir / "orders.csv", index=False, encoding="utf-8")

    # 信号日志（观察用）
    log_df = pd.DataFrame(strategy.signal_log)
    log_df.to_csv(date_dir / "signal_log.csv", index=False, encoding="utf-8")

    # 摘要
    summary = {
        "as_of": as_of.isoformat(),
        "strategy": "hype_watch",
        "mode": strategy._mode,
        "pool_size": params.pool_size,
        "weights": {k: round(v, 4) for k, v in weights.items()},
        "target_shares": target_shares,
        "capital": round(capital, 2),
        "n_orders": len(orders),
        "circuit_flat": strategy._circuit_flat,
        "circuit_reason": strategy._circuit_reason,
        "warnings": strategy.warnings,
        "disclaimer": PAPER_DISCLAIMER,
    }
    (date_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    # 更新权益历史（给断路器用）
    # 用 最新市值 + 现金 估算权益
    portfolio_value = sum(
        int(target_shares.get(s, 0)) * prices.get(s, 0) for s in target_shares)
    cash_left = capital - portfolio_value
    est_equity = portfolio_value + max(cash_left, 0)
    _append_equity(eq_path, as_of, est_equity)

    # ---- 打印 ----
    print(f"\n策略 hype_watch  信号日 {as_of.date()}  模式 {strategy._mode}  "
          f"资金 {capital:,.0f}")
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

    if args.dry_run:
        print("\n--dry-run：未写文件。")
        return

    print(f"\n已写入 {date_dir}")
    print(f"委托 {len(orders)} 笔（orders.csv）")


if __name__ == "__main__":
    main()
