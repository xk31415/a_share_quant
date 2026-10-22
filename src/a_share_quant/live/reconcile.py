"""成交对账：把券商成交回报和模型委托单逐笔比对。

对账要回答的不是"今天赚了多少"，而是三个更基础的问题：

1. **执行偏差**：委托 1000 股，实际成交多少？没成交的是因为跌停/停牌，还是漏下了？
2. **滑点**：实际成交价比信号日收盘价差多少 bp？这是实盘相对回测的衰减来源。
3. **结果偏差**：成交后的持仓与目标持仓差多少？——这条最重要，前两条都是它的解释。

设计原则：**不猜**。查不到对应委托的成交（可能是手动单）、成交股数超过委托、
卖出超过持仓，全部单独列出并报错，不静默并入统计。
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

import pandas as pd

# 券商导出的方向字段五花八门，统一到 buy/sell
SIDE_ALIASES = {
    "buy": "buy", "b": "buy", "买": "buy", "买入": "buy", "证券买入": "buy",
    "担保品买入": "buy", "融资买入": "buy",
    "sell": "sell", "s": "sell", "卖": "sell", "卖出": "sell", "证券卖出": "sell",
    "担保品卖出": "sell", "融券卖出": "sell",
}

ORDER_COLUMNS = ["symbol", "side", "shares", "price"]
FILL_COLUMNS = ["symbol", "side", "shares", "price"]

# 券商中文导出的列名。只收"成交"口径：委托数量/委托价格不是成交，不能映射。
# 费用只认**合计**列——手续费/佣金在不同券商里时含时不含印花税、过户费，猜了就错。
FILL_COLUMN_ALIASES = {
    "symbol": ("证券代码", "股票代码", "代码"),
    "side": ("买卖标志", "买卖方向", "操作", "委托类别"),
    "shares": ("成交数量", "成交股数"),
    "price": ("成交价格", "成交均价"),
    "fee": ("费用合计", "交易费用", "总费用"),
}
FILL_SYMBOL_COLUMNS = ("symbol",) + FILL_COLUMN_ALIASES["symbol"]


class ReconcileError(ValueError):
    """对账发现必须人工处理的问题（方向错、超卖、对不上单）。"""


def normalize_side(value) -> str:
    key = str(value).strip().lower()
    key = SIDE_ALIASES.get(key, SIDE_ALIASES.get(str(value).strip(), None))
    if key is None:
        raise ReconcileError(f"无法识别的买卖方向：{value!r}")
    return key


def _rename_broker_columns(fills: pd.DataFrame) -> pd.DataFrame:
    """中文列名 → 标准列名。同一目标出现两个候选列时报错，不猜用哪个。"""
    cols = [str(c).strip() for c in fills.columns]
    rename = {}
    for target, aliases in FILL_COLUMN_ALIASES.items():
        hits = [c for c in (target,) + aliases if c in cols]
        if len(hits) > 1:
            raise ReconcileError(f"成交表里 {hits} 都能当作 {target}，无法确定用哪一列；"
                                 f"删掉多余的列或改成标准列名 {target}")
        if hits and hits[0] != target:
            rename[hits[0]] = target
    out = fills.copy()
    out.columns = cols
    return out.rename(columns=rename)


def normalize_fills(fills: pd.DataFrame) -> pd.DataFrame:
    """校验并规范化成交表。

    必需列：symbol / side / shares / price；可选 fee。也认券商中文导出的列名
    （见 ``FILL_COLUMN_ALIASES``），规范化后的结果与标准列名完全一致，
    因此重复入账指纹不随导出模板变化。
    返回按 (symbol, side) 排好序的新表，**不修改入参**。
    """
    fills = _rename_broker_columns(fills)
    missing = set(FILL_COLUMNS) - set(fills.columns)
    if missing:
        raise ReconcileError(f"成交表缺列：{sorted(missing)}")

    out = fills.copy()
    out["symbol"] = out["symbol"].astype(str).str.strip()
    out["side"] = [normalize_side(x) for x in out["side"]]
    for col in ("shares", "price"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if out["shares"].isna().any() or out["price"].isna().any():
        bad = out[out["shares"].isna() | out["price"].isna()]
        raise ReconcileError(f"成交表有 {len(bad)} 行股数/价格无法解析："
                             f"{bad['symbol'].head(5).tolist()}")
    frac = out["shares"] != out["shares"].round()
    if frac.any():
        raise ReconcileError(f"成交股数不是整数：{out.loc[frac, 'symbol'].head(5).tolist()}"
                             f"（可能把成交金额当成了数量）")
    neg = out["shares"] < 0
    if neg.any():
        raise ReconcileError(f"成交股数为负数：{out.loc[neg, 'symbol'].head(5).tolist()}。"
                             f"方向请放在 side 列，股数一律写正数")
    out["shares"] = out["shares"].astype(int)
    out = out[out["shares"] > 0]
    if "fee" in out.columns:
        out["fee"] = pd.to_numeric(out["fee"], errors="coerce").fillna(0.0)
    else:
        out["fee"] = float("nan")
    return out.sort_values(["symbol", "side"]).reset_index(drop=True)


def aggregate_fills(fills: pd.DataFrame) -> pd.DataFrame:
    """同一 (symbol, side) 的多笔成交合并成一笔，价格按股数加权。"""
    if fills.empty:
        return pd.DataFrame(columns=["symbol", "side", "shares", "price", "fee"])
    f = fills.copy()
    f["_amt"] = f["shares"] * f["price"]
    g = f.groupby(["symbol", "side"], as_index=False).agg(
        shares=("shares", "sum"), _amt=("_amt", "sum"), fee=("fee", "sum"))
    g["price"] = g["_amt"] / g["shares"]
    return g.drop(columns=["_amt"])


def _slip_bps(side: str, order_price: float, fill_price: float) -> float:
    """不利偏移（bp）。正数 = 比委托价吃亏，负数 = 占便宜。

    买入成交价高于参考价是不利，卖出低于参考价是不利，所以方向要取反。
    """
    if not order_price or order_price <= 0:
        return float("nan")
    raw = (fill_price / order_price - 1.0) * 1e4
    return raw if side == "buy" else -raw


def match_orders(orders: pd.DataFrame, fills: pd.DataFrame,
                 lot_size: int = 100) -> tuple[pd.DataFrame, pd.DataFrame]:
    """逐 (symbol, side) 比对，返回 (匹配表, 无对应委托的成交)。

    匹配表的 status：
      * ``filled``  成交股数 == 委托股数
      * ``partial`` 部分成交（跌停/停牌/资金不足的典型表现）
      * ``missing`` 完全没成交
      * ``over``    成交超过委托——**必须人工确认**，可能是手动补单
    """
    miss = set(ORDER_COLUMNS) - set(orders.columns)
    if miss:
        raise ReconcileError(f"委托表缺列：{sorted(miss)}")

    o = orders.copy()
    o["symbol"] = o["symbol"].astype(str).str.strip()
    o["side"] = [normalize_side(x) for x in o["side"]]
    o["shares"] = pd.to_numeric(o["shares"], errors="coerce").fillna(0).astype(int)
    o["price"] = pd.to_numeric(o["price"], errors="coerce")
    # 0 股的委托是"无法成交"的记录，不参与对账
    o = o[o["shares"] > 0]

    agg = aggregate_fills(fills)
    merged = o.merge(agg, on=["symbol", "side"], how="outer",
                     suffixes=("_order", "_fill"), indicator=True)

    unmatched = merged[merged["_merge"] == "right_only"].copy()
    matched = merged[merged["_merge"] != "right_only"].copy()

    matched["order_shares"] = matched["shares_order"].fillna(0).astype(int)
    matched["fill_shares"] = matched["shares_fill"].fillna(0).astype(int)
    matched["order_price"] = matched["price_order"]
    matched["fill_price"] = matched["price_fill"]
    matched["fee"] = matched["fee"].fillna(0.0)

    def status(row) -> str:
        if row["fill_shares"] == 0:
            return "missing"
        if row["fill_shares"] > row["order_shares"]:
            return "over"
        if row["fill_shares"] < row["order_shares"]:
            return "partial"
        return "filled"

    matched["status"] = matched.apply(status, axis=1)
    matched["fill_ratio"] = (matched["fill_shares"] /
                             matched["order_shares"].replace(0, pd.NA)).astype(float)
    matched["slip_bps"] = [
        _slip_bps(s, op, fp) if pd.notna(op) and pd.notna(fp) else float("nan")
        for s, op, fp in zip(matched["side"], matched["order_price"],
                             matched["fill_price"])
    ]
    matched["shares_gap"] = matched["fill_shares"] - matched["order_shares"]

    cols = ["symbol", "side", "order_shares", "order_price", "fill_shares",
            "fill_price", "shares_gap", "fill_ratio", "slip_bps", "fee", "status"]
    extra = unmatched[["symbol", "side", "shares_fill", "price_fill", "fee"]] \
        .rename(columns={"shares_fill": "shares", "price_fill": "price"}) \
        .reset_index(drop=True)
    return (matched[cols].sort_values(["status", "symbol"]).reset_index(drop=True),
            extra)


def apply_fills(holdings: Mapping[str, int], fills: pd.DataFrame,
                lot_size: int = 100) -> tuple[dict[str, int], list[str]]:
    """用成交更新持仓，返回 (新持仓, 告警列表)。

    告警而非静默处理的情况：
      * 卖出超过持仓（可能是融资券/数据错误，必须人工核对）
      * 卖出股数不是整手且非清仓（违反上证函〔2014〕301 号）
      * 买入股数不是整手（A 股买入必须整百）
    """
    pos = {str(k): int(v) for k, v in holdings.items() if int(v) > 0}
    warnings: list[str] = []

    if fills.empty:
        return pos, warnings

    for row in aggregate_fills(fills).itertuples(index=False):
        sym, side, sh = str(row.symbol), str(row.side), int(row.shares)
        if side == "buy":
            if sh % lot_size:
                warnings.append(f"{sym} 买入 {sh} 股不是 {lot_size} 的整数倍")
            pos[sym] = pos.get(sym, 0) + sh
        else:
            held = pos.get(sym, 0)
            if sh > held:
                warnings.append(f"{sym} 卖出 {sh} 股超过持仓 {held} 股")
            left = held - sh
            if left < 0:
                left = 0
            if left > 0 and sh % lot_size:
                warnings.append(
                    f"{sym} 卖出 {sh} 股不是整手，且未清仓（剩余 {left} 股）——"
                    f"违反上证函〔2014〕301 号")
            if left == 0:
                pos.pop(sym, None)
            else:
                pos[sym] = left
    return {k: v for k, v in sorted(pos.items()) if v > 0}, warnings


def compare_holdings(actual: Mapping[str, int], target: pd.DataFrame,
                     prices: Optional[Mapping[str, float]] = None,
                     lot_size: int = 100) -> pd.DataFrame:
    """成交后持仓 vs 目标持仓。

    ``target`` 用 ``live/<日期>/target.csv``（含 symbol / target_shares / price）。
    返回按偏离金额降序的表——**金额比股数更能说明问题**。
    """
    need = {"symbol", "target_shares"}
    if not need.issubset(target.columns):
        raise ReconcileError(f"目标表缺列：{sorted(need - set(target.columns))}")

    t = target.copy()
    t["symbol"] = t["symbol"].astype(str)
    price_map = dict(prices) if prices is not None else dict(
        zip(t["symbol"], pd.to_numeric(t.get("price"), errors="coerce")))
    tgt = dict(zip(t["symbol"], pd.to_numeric(t["target_shares"], errors="coerce")
                   .fillna(0).astype(int)))

    rows = []
    for sym in sorted(set(tgt) | set(actual)):
        want = int(tgt.get(sym, 0))
        have = int(actual.get(sym, 0))
        px = price_map.get(sym)
        px = float(px) if px is not None and pd.notna(px) else float("nan")
        rows.append({
            "symbol": sym,
            "target_shares": want,
            "actual_shares": have,
            "shares_gap": have - want,
            "price": px,
            "value_gap": (have - want) * px if pd.notna(px) else float("nan"),
            # 差不到一手就是执行误差，不算真偏离
            "material": abs(have - want) >= lot_size,
        })
    out = pd.DataFrame(rows)
    return out.sort_values("value_gap", key=lambda s: s.abs(),
                           ascending=False).reset_index(drop=True)


def summarize(matched: pd.DataFrame, unmatched: pd.DataFrame,
              holdings_cmp: Optional[pd.DataFrame] = None,
              model_fee: Optional[float] = None) -> dict:
    """汇总指标。所有比率在分母为 0 时返回 0，不返回 NaN 混进报告。"""
    if matched.empty:
        n = {"n_orders": 0, "n_filled": 0, "n_partial": 0, "n_missing": 0,
             "n_over": 0, "fill_ratio": 0.0}
        # 分母为 0 时返回 0，不返回 NaN——NaN 会被 json.dumps 写成非标准
        # 字面量，严格 JSON 解析器（JS/R）读不了这个文件（R06 F-31）。
        slip_bps = fee_bps = 0.0
        total_fee = 0.0
    else:
        counts = matched["status"].value_counts().to_dict()
        ordered = float(matched["order_shares"].sum())
        n = {
            "n_orders": int(len(matched)),
            "n_filled": int(counts.get("filled", 0)),
            "n_partial": int(counts.get("partial", 0)),
            "n_missing": int(counts.get("missing", 0)),
            "n_over": int(counts.get("over", 0)),
            "fill_ratio": float(matched["fill_shares"].sum() / ordered)
            if ordered else 0.0,
        }
        filled = matched[matched["fill_shares"] > 0]
        notional = float((filled["fill_shares"] * filled["fill_price"]).sum())
        slip_bps = (float((filled["slip_bps"] * filled["fill_shares"] *
                           filled["fill_price"]).sum() / notional)
                    if notional > 0 else 0.0)
        # 无对应委托的成交也是真花了钱，必须计入总费用——
        # 否则"实际 vs 模型"的差额会被这部分抵消，看起来反而更省。
        unmatched_fee = float(unmatched["fee"].sum()) \
            if unmatched is not None and "fee" in unmatched.columns else 0.0
        total_fee = float(matched["fee"].sum()) + unmatched_fee
        fee_bps = (total_fee / notional * 1e4) if notional > 0 else 0.0

    out = dict(n)
    out["total_fee"] = total_fee
    out["fee_bps"] = fee_bps
    out["weighted_slip_bps"] = slip_bps
    out["n_unmatched_fills"] = int(len(unmatched))

    if model_fee is not None and total_fee > 0:
        # 实际费用 vs 模型预期：差得多说明费率表需要更新
        out["model_fee"] = float(model_fee)
        out["fee_vs_model"] = float(total_fee - model_fee)

    if holdings_cmp is not None and not holdings_cmp.empty:
        material = holdings_cmp[holdings_cmp["material"]]
        out["n_holdings_diff"] = int(len(material))
        out["abs_value_gap"] = float(holdings_cmp["value_gap"].abs().sum())
        out["max_value_gap"] = float(holdings_cmp["value_gap"].abs().max())
    return out
