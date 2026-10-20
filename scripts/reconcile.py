"""成交对账：拿券商成交回报核对模型委托单，并生成下一次要读的持仓表。

用法
----
    python scripts/reconcile.py \
        --orders live/2026-10-20/orders.csv \
        --fills fills/2026-10-21.csv \
        --holdings live/holdings.csv \
        --save-holdings live/holdings.csv

成交表（CSV）最少四列，可选 `fee`：

    symbol,side,shares,price[,fee]
    600519,buy,100,1331.88,5.00
    000001,sell,150,11.44,5.09

`side` 接受 `buy/sell`、`买入/卖出`、`证券买入/证券卖出` 等常见写法。

产出
----
    live/<日期>/reconcile.json   汇总指标
    live/<日期>/orders_vs_fills.csv  逐笔比对
    live/<日期>/holdings_gap.csv     成交后持仓 vs 目标持仓

退出码：0 正常；2 有需要人工处理的问题（超卖、成交多于委托、对不上单）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import yaml

from a_share_quant.live.reconcile import (
    ReconcileError,
    apply_fills,
    compare_holdings,
    match_orders,
    normalize_fills,
    summarize,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_csv(path: Path, what: str) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"找不到{what}：{path}")
    return pd.read_csv(path, dtype={"symbol": str})


def _load_holdings(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        print(f"[warn] 持仓文件不存在（{p}），按空仓对账", flush=True)
        return {}
    df = pd.read_csv(p, dtype={"symbol": str})
    if not {"symbol", "shares"}.issubset(df.columns):
        raise SystemExit(f"持仓文件缺列：需要 symbol,shares（{p}）")
    out = {}
    for sym, sh in zip(df["symbol"], pd.to_numeric(df["shares"], errors="coerce")):
        if pd.isna(sh) or int(sh) <= 0:
            continue
        out[str(sym).strip()] = int(sh)
    return out


def _model_fee(fills: pd.DataFrame, costs: dict) -> float:
    """按配置的费率表估算总费用，用来和实际费用对照。"""
    if fills.empty:
        return 0.0
    cr = float(costs.get("commission_rate", 0.00025))
    cmin = float(costs.get("commission_min", 5.0))
    stamp = float(costs.get("stamp_duty_rate", 0.0005))
    total = 0.0
    for row in fills.itertuples(index=False):
        amt = float(row.shares) * float(row.price)
        total += max(amt * cr, cmin)
        if row.side == "sell":
            total += amt * stamp
    return total


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orders", required=True,
                    help="模型委托单：orders.csv 或包含它的目录")
    ap.add_argument("--fills", required=True, help="券商成交回报 CSV")
    ap.add_argument("--holdings", default=None,
                    help="**成交前**的持仓（symbol,shares）。别传成交后的——"
                         "会把同一批成交算两遍，持仓直接翻倍")
    ap.add_argument("--save-holdings", default=None,
                    help="把成交后的持仓写到这个路径（供下次 live_signal 读）")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--out", default=None,
                    help="输出目录（默认与 orders.csv 同级）")
    ap.add_argument("--lot-size", type=int, default=100)
    args = ap.parse_args()

    orders_path = Path(args.orders)
    if orders_path.is_dir():
        orders_path = orders_path / "orders.csv"
    if not orders_path.exists():
        raise SystemExit(f"找不到委托单：{orders_path}")
    base = orders_path.parent
    out_dir = Path(args.out) if args.out else base
    out_dir.mkdir(parents=True, exist_ok=True)

    orders = _read_csv(orders_path, "委托单")
    fills_raw = _read_csv(Path(args.fills), "成交回报")
    holdings = _load_holdings(args.holdings)
    target_path = base / "target.csv"
    target = _read_csv(target_path, "目标持仓") if target_path.exists() else None

    try:
        fills = normalize_fills(fills_raw)
        matched, unmatched = match_orders(orders, fills, args.lot_size)
        new_holdings, warn_hold = apply_fills(holdings, fills, args.lot_size)
    except ReconcileError as exc:
        raise SystemExit(f"对账失败：{exc}") from exc

    gap = compare_holdings(new_holdings, target, lot_size=args.lot_size) \
        if target is not None else None

    cfg = Path(args.config)
    costs = {}
    if cfg.exists():
        costs = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get(
            "costs", {}) or {}
    summary = summarize(matched, unmatched, gap, model_fee=_model_fee(fills, costs))

    # ---- 输出 ----
    print(f"委托 {summary['n_orders']} 笔 / 成交 {summary['n_filled']} 笔"
          f"（部分 {summary['n_partial']}、未成交 {summary['n_missing']}、"
          f"超量 {summary['n_over']}）")
    print(f"股数成交率 {summary['fill_ratio']:.2%}   "
          f"加权滑点 {summary['weighted_slip_bps']:+.1f}bp   "
          f"费用 {summary['total_fee']:,.0f} 元（{summary['fee_bps']:.1f}bp）")
    if "model_fee" in summary:
        print(f"模型预估费用 {summary['model_fee']:,.0f} 元，"
              f"实际差 {summary['fee_vs_model']:+,.0f} 元")

    problems: list[str] = []
    if summary["n_missing"]:
        rows = matched[matched["status"] == "missing"]
        problems.append(f"{len(rows)} 笔完全未成交：")
        for r in rows.head(10).itertuples(index=False):
            problems.append(f"    {r.symbol} {r.side} 委托 {r.order_shares} 股")
    if summary["n_partial"]:
        rows = matched[matched["status"] == "partial"]
        problems.append(f"{len(rows)} 笔部分成交：")
        for r in rows.head(10).itertuples(index=False):
            problems.append(f"    {r.symbol} {r.side} 委托 {r.order_shares} "
                            f"成交 {r.fill_shares}（缺 {abs(r.shares_gap)}）")
    if summary["n_over"]:
        rows = matched[matched["status"] == "over"]
        problems.append(f"{len(rows)} 笔成交超过委托（可能是手动补单，需人工确认）：")
        for r in rows.head(10).itertuples(index=False):
            problems.append(f"    {r.symbol} {r.side} 委托 {r.order_shares} "
                            f"成交 {r.fill_shares}（多 {abs(r.shares_gap)}）")
    if len(unmatched):
        problems.append(f"{len(unmatched)} 笔成交找不到对应委托：")
        for r in unmatched.head(10).itertuples(index=False):
            problems.append(f"    {r.symbol} {r.side} {r.shares} 股 @ {r.price}")
    problems.extend(warn_hold)

    if gap is not None:
        material = gap[gap["material"]]
        print(f"持仓偏离：{len(material)} 只差 ≥{args.lot_size} 股，"
              f"绝对偏离 {summary['abs_value_gap']:,.0f} 元"
              f"（最大 {summary['max_value_gap']:,.0f} 元）")
        if len(material):
            problems.append(f"{len(material)} 只持仓与目标差 ≥{args.lot_size} 股：")
            for r in material.head(10).itertuples(index=False):
                problems.append(f"    {r.symbol} 目标 {r.target_shares} "
                                f"实际 {r.actual_shares}（{r.shares_gap:+d}）")

    if problems:
        print("\n需要处理：")
        for p in problems:
            print("  " + p)

    matched.to_csv(out_dir / "orders_vs_fills.csv", index=False, encoding="utf-8")
    if gap is not None:
        gap.to_csv(out_dir / "holdings_gap.csv", index=False, encoding="utf-8")
    if unmatched is not None and len(unmatched):
        unmatched.to_csv(out_dir / "unmatched_fills.csv", index=False,
                         encoding="utf-8")
    (out_dir / "reconcile.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=float),
        encoding="utf-8")

    if args.save_holdings:
        p = Path(args.save_holdings)
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"symbol": list(new_holdings),
                      "shares": list(new_holdings.values())}) \
            .to_csv(p, index=False, encoding="utf-8")
        print(f"\n新持仓（{len(new_holdings)} 只）已写入 {p}")

    print(f"明细已写入 {out_dir}")
    # 超卖/超量/对不上单属于必须人工处理的问题，用非零退出码让脚本链能感知
    critical = (summary["n_over"] > 0 or len(unmatched) > 0
                or any("超过持仓" in w for w in warn_hold))
    sys.exit(2 if critical else 0)


if __name__ == "__main__":
    main()
