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
券商中文导出（证券代码/买卖标志/成交数量/成交价格/费用合计，UTF-8 或 GBK）
可以直接用；同一目标出现两个候选列（如成交价格和成交均价）时拒绝，不猜。
`--holdings` 指向的文件必须存在；首次建仓不传 `--holdings`，加 `--from-empty`。

产出
----
    live/<日期>/reconcile.json   汇总指标
    live/<日期>/orders_vs_fills.csv  逐笔比对
    live/<日期>/holdings_gap.csv     成交后持仓 vs 目标持仓

退出码：0 正常；1 这批成交已经入过账（见下方"重复入账"），**只有这一种情况**；
2 需要人工处理：超卖、成交多于委托、对不上单，以及一切输入错误（文件不存在、
缺列、列名歧义、`--save-holdings` 已存在却没传 `--holdings`）。
**链式脚本遇到非 0 必须停下**：1 和 2 都没有写持仓。

重复入账（R06 F-26，R07 F-32/F-33/F-34 修正）
---------------------------------------------
`--save-holdings` 的输出通常是**次日建仓的输入**。同一份成交再跑一次会把股数
再加一遍（1000 → 1100 → 1200），两次退出码都是 0，错得很隐蔽。因此：

* 入账后把**股数指纹**追加进 `<holdings>.digest` 的**历史列表**（保留最近 120 条）；
* 再跑同一批成交 → 退出码 1，拒绝写盘，**也不改动 out_dir 下的报告**
  （报告写盘曾经在拦截之后，会把按重复入账算出的偏离覆盖到正确报告上，R07 F-34）；
* 指纹**只取 (symbol, side, 股数)**：列顺序、多余列（成交编号）、dtype、
  明细/汇总粒度、成交价的两分钱四舍五入都不影响（R07 F-32、R08 F-36）。
  价格不进指纹是刻意的——决定持仓变化的硬信息只有股数；
* **日期不参与判定**，`--as-of` 只写进历史供人看。R07 曾把日期做进判定键，
  结果改一下 `--as-of` 就能把同一批连加三次（R08 F-37）；
* 判的是**历史**而不是"最后一批"：`h0 -A→ h1 -B→ h2` 之后拿 h2 重跑 A 也会被拦；
* **空批次不记指纹**：它本来就没有可重复入账的股数，记了会让第二天同样的
  空成交被误判（R07 F-33）；
* 同日分两次回报且股数恰好相同时，加 `--force` 照常入账。**不要删 `.digest`
  文件来解除**——那会废掉这份持仓的保护（R07 F-33）；
* 存在必须人工处理的问题（退出码 2）时**不写持仓**——报警不该先落一份错账。

指纹只防"同一批成交被再对一次"这一种错误。改单、分红送股、漏对账它都防不住，
所以**每周至少一次要用券商"持仓查询"导出逐只核对股数**。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from a_share_quant.live.reconcile import (
    FILL_SYMBOL_COLUMNS,
    ReconcileError,
    aggregate_fills,
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
    dtype = {c: str for c in FILL_SYMBOL_COLUMNS}
    try:
        return pd.read_csv(path, dtype=dtype, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, dtype=dtype, encoding="gbk")


def _load_holdings(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        # 路径打错时按空仓对账，全是买入的那天退出码 0，真实持仓被整份盖掉。
        raise SystemExit(f"持仓文件不存在：{p}。路径打错了？"
                         f"成交前确实空仓就不要传 --holdings，改加 --from-empty")
    df = pd.read_csv(p, dtype={"symbol": str})
    if not {"symbol", "shares"}.issubset(df.columns):
        raise SystemExit(f"持仓文件缺列：需要 symbol,shares（{p}）")
    out = {}
    for sym, sh in zip(df["symbol"], pd.to_numeric(df["shares"], errors="coerce")):
        if pd.isna(sh) or int(sh) <= 0:
            continue
        out[str(sym).strip()] = int(sh)
    return out


def _fills_digest(fills: pd.DataFrame) -> str:
    """这批成交的**股数指纹**，只取 (symbol, side, 股数)。

    三次修正的净结果，每一次都是被真实绕过路径逼出来的：

    1. 拿整个 DataFrame 哈希 → 换列顺序、多一列成交编号、dtype（`10` vs
       `10.0`）、明细 vs 汇总全部会变，换一种券商导出模板就认不出（R07 F-32）；
    2. 加上价格、`round(3)` 之后仍然不够：两笔 100 股 @10.01 与 @10.02
       加权是 10.015，而券商「交割单」按两位小数写成一行 `200 @ 10.02`，
       指纹又对不上，股数被再加一遍（R08 F-36）；
    3. 所以**价格整个不进指纹**。决定持仓变化的硬信息只有股数——价格差一分钱
       不该让同一批成交变成"另一批"。代价是同日同股数、不同价的合法成交
       会被拦，那种情况用 `--force` 放行。

    `fee` 也不进指纹，但**费用对照仍用原始 `fills`**（`_model_fee`）。
    """
    if fills.empty:
        return ""
    agg = aggregate_fills(fills)
    core = pd.DataFrame({
        "symbol": agg["symbol"].astype(str).str.strip(),
        "side": agg["side"].astype(str).str.strip(),
        "shares": agg["shares"].astype("int64"),
    })
    ordered = core.sort_values(by=["symbol", "side"],
                               kind="mergesort").reset_index(drop=True)
    h = pd.util.hash_pandas_object(ordered, index=False)
    return hashlib.sha1(h.values.tobytes()).hexdigest()


# 历史保留多少条。够覆盖"8 周 × 每天 1~2 批"，超出就丢最旧的。
_HISTORY_KEEP = 120


def _digest_history(path: Path) -> set[str]:
    """读过已入账的指纹集合。

    存成**历史列表**而不是"最后一批"，是为了堵两个洞：
    - 只记最后一批时，`h0 -A→ h1 -B→ h2` 之后拿 h2 重跑 A 不拦（洞 ①）；
    - 拿日期当判定键时，改 `--as-of` 就能把同一批连加三次（R08 F-37）。

    每行格式 `YYYY-MM-DD <指纹>`，日期只为人看，**判定只比对指纹**。
    """
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            out.add(parts[-1])
    return out


def _append_digest(path: Path, as_of: str, digest: str) -> None:
    """把这次入账的指纹追加进历史（保留最近 `_HISTORY_KEEP` 条）。"""
    lines = []
    if path.exists():
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    lines.append(f"{as_of} {digest}")
    path.write_text("\n".join(lines[-_HISTORY_KEEP:]) + "\n", encoding="utf-8")


def _infer_as_of(base: Path) -> str:
    """从委托单所在目录名推断日期（`live/<日期>/`）。推断不出就 `unknown`。

    这个日期**只写进历史供人看，不参与重复判定**——R07 曾把它做进判定键，
    结果改一下 `--as-of` 就能绕过（R08 F-37）。
    """
    try:
        datetime.strptime(base.name, "%Y-%m-%d")
    except ValueError:
        return "unknown"
    return base.name


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
    ap.add_argument("--from-empty", action="store_true",
                    help="确认**成交前确实空仓**。仅在 --save-holdings 指向一个"
                         "已存在的文件、又没传 --holdings 时需要")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--out", default=None,
                    help="输出目录（默认与 orders.csv 同级）")
    ap.add_argument("--lot-size", type=int, default=100)
    ap.add_argument("--as-of", default=None,
                    help="对账日期（默认取委托单所在目录名）。参与重复入账判定，"
                         "跨天同内容的成交不算重复")
    ap.add_argument("--force", action="store_true",
                    help="确认这批成交是**另一批真实成交**（同日分两次回报且内容相同），"
                         "即使指纹相同也照常入账。不要靠删 digest 文件来解除——"
                         "那会废掉这份持仓的保护")
    args = ap.parse_args()

    orders_path = Path(args.orders)
    if orders_path.is_dir():
        orders_path = orders_path / "orders.csv"
    if not orders_path.exists():
        raise SystemExit(f"找不到委托单：{orders_path}")
    base = orders_path.parent
    out_dir = Path(args.out) if args.out else base
    out_dir.mkdir(parents=True, exist_ok=True)
    as_of = args.as_of or _infer_as_of(base)

    # R08 F-39：`--save-holdings` 指向已存在的持仓、又没传 `--holdings` 时，
    # 按空仓对账会把整份持仓盖成"这一笔成交的结果"。成交全是买入的那天
    # 不会触发超卖保护，退出码是 0，标准输出也没有任何提示——实测
    # `600000,1000 / 000001,500` 被盖成 `600000,100`，1000 股和 500 股一起没了。
    # 「没传参数」和「确实是空仓」必须区分开。
    if (args.save_holdings and not args.holdings and not args.from_empty
            and Path(args.save_holdings).exists()):
        raise SystemExit(
            f"{args.save_holdings} 已存在，但没有传 --holdings。\n"
            f"按空仓对账会把这份持仓整个盖掉（成交全是买入时退出码还是 0）。\n"
            f"  · 用现有持仓当成交前持仓：--holdings {args.save_holdings}\n"
            f"  · 成交前确实是空仓：加 --from-empty")

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

    # ---- 重复入账检查：必须在写**任何**文件之前 ----
    # 报告写盘原来在拦截之后，于是被拦的重跑会把"按重复入账算出的持仓偏离"
    # 写进 holdings_gap.csv / reconcile.json，覆盖第一次的正确版本（R07 F-34）。
    # 持仓文件保住了，给人看的报告却被改成一份并不存在的偏离，且无法恢复。
    dg = _fills_digest(fills)
    # 只要传了 --holdings，就查它的指纹历史——**跟有没有 --save-holdings 无关**。
    # 原来的条件是 `args.save_holdings and dg`，于是「不写持仓但会写报告」的那次
    # 重跑完全不查指纹：退出码 0，`n_holdings_diff` 从 0 变成 2、
    # `abs_value_gap` 从 0 变成 3300，正确报告被按重复入账算出的偏离盖掉（R08 F-38）。
    if dg and args.holdings:
        # 查的是**输入**持仓的指纹历史，不是输出文件的：要判断的是
        # "这批成交是否已经被这份持仓吸收过"。查输出文件在链式用法
        # （h1 → h2）下永远是空的，保护形同虚设。
        prev = Path(str(args.holdings) + ".digest") if args.holdings else None
        if (prev is not None and not args.force
                and dg in _digest_history(prev)):
            print(f"\n这批成交已经入过账（指纹 {dg[:12]}，见 {prev.name}）。\n"
                  f"再跑一遍会把股数加第二遍，因此**拒绝写盘**，"
                  f"也不会改动 {out_dir} 下的报告。\n"
                  f"如果这确实是**另一批真实成交**（同日分两次回报、股数恰好相同），"
                  f"加 --force 照常入账。\n"
                  f"不要删 {prev.name} 来解除——那会废掉这份持仓的保护。",
                  flush=True)
            sys.exit(1)

    # 超卖/超量/对不上单属于必须人工处理的问题，用非零退出码让脚本链能感知。
    critical = (summary["n_over"] > 0 or len(unmatched) > 0
                or any("超过持仓" in w for w in warn_hold))

    # ---- 写报告（退出码 2 时保留，作为人工核对的诊断材料）----
    matched.to_csv(out_dir / "orders_vs_fills.csv", index=False, encoding="utf-8")
    # 没有就不留旧文件：残留的旧表会和 reconcile.json 的计数互相矛盾，
    # 有人会照着它去追查一笔已经排除的成交（R07 F-35）。
    for name, obj in (("holdings_gap.csv", gap),
                      ("unmatched_fills.csv", unmatched)):
        p = out_dir / name
        if obj is not None and len(obj):
            obj.to_csv(p, index=False, encoding="utf-8")
        elif p.exists():
            p.unlink()
    (out_dir / "reconcile.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=float),
        encoding="utf-8")

    if args.save_holdings:
        if critical:
            print(f"\n[warn] 存在必须人工处理的问题，未写入 {args.save_holdings}。"
                  f"先核对成交，再决定是否入账", flush=True)
        else:
            p = Path(args.save_holdings)
            p.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame({"symbol": list(new_holdings),
                          "shares": list(new_holdings.values())}) \
                .to_csv(p, index=False, encoding="utf-8")
            if dg:
                # 空批次不记指纹：它本来就没有可重复入账的股数，记了会让
                # 第二天同样的空成交被误判成重复（R07 F-33）。
                digest_path = Path(str(args.save_holdings) + ".digest")
                _append_digest(digest_path, as_of, dg)
                print(f"成交指纹 {dg[:12]} 已记入 {digest_path.name}（防重复入账）",
                      flush=True)
            print(f"\n新持仓（{len(new_holdings)} 只）已写入 {p}")

    print(f"明细已写入 {out_dir}")
    sys.exit(2 if critical else 0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        # `raise SystemExit("说明")` 默认退出码 1，会和"已入过账"混淆；
        # 输入错误一律按 2（人工处理）退出。
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr, flush=True)
            sys.exit(2)
        raise
