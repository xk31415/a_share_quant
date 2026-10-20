"""因子研究：IC、分档单调性、流动性分档内的长仓绝对收益。

口径说明（重要）：

* **IC 用原始因子值算**，不做方向调整。IC 为正表示"因子值越大，下月收益越高"；
  负号只影响解读，不影响统计量。
* **多空价差** = 原始因子第 10 档月均收益 - 第 1 档月均收益，年化。
  IC 与该价差同号才说明"分档单调"，否则是尾部效应。
* **长仓绝对收益**：A 股不能便捷做空，真正能用的是"买第 1 档"。所以每个
  流动性档位里同时报 top / bottom 的绝对年化收益，以及该档位的**等权对照**。
  只有 top 明显高于同档等权，才算有选股能力。

为什么必须先分流动性档：本项目已确认全宇宙等权 8.72%/年，而成交额最低的
1/5 是 +22.2%、最高的 1/5 是 -4.6%（见 reports/strategy_log.md 第 0 节）。
不分档就会把流动性溢价当成因子收益。

用法：
    python scripts/factor_research.py --data-dir data/processed
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


# ------------------------------------------------------------------ 数据
def load_matrices(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回 (后复权收盘价, 日收益, 成交额)，均为 date x symbol 矩阵。"""
    bars = pd.read_parquet(data_dir / "bars.parquet")
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars[(bars["is_suspended"] == False) & bars["adj_close"].notna()
                & (bars["adj_close"] > 0)]
    px = bars.pivot_table(index="trade_date", columns="symbol", values="adj_close")
    amt = bars.pivot_table(index="trade_date", columns="symbol", values="amount")
    return px, px.pct_change(), amt


# ------------------------------------------------------------------ 因子
FACTORS = {
    "mom_5": "过去 5 日收益",
    "mom_20": "过去 20 日收益",
    "mom_60": "过去 60 日收益",
    "vol_20": "20 日波动率",
    "vol_60": "60 日波动率",
    "max_ret_20": "过去 20 日最大单日涨幅（彩票效应）",
    "log_amt_20": "20 日均成交额对数",
    "illiq_20": "Amihud 非流动性 log(mean(|r|/amount))",
}


def factor_panel(px: pd.DataFrame, ret: pd.DataFrame,
                 amt: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    """截至 t 日收盘可得的因子值（严格只用 t 及之前的数据）。"""
    hist_px, hist_ret, hist_amt = px.loc[:t], ret.loc[:t], amt.loc[:t]
    if len(hist_px) < 61:
        return pd.DataFrame()

    f = pd.DataFrame(index=px.columns)
    f["mom_5"] = hist_px.iloc[-1] / hist_px.iloc[-6] - 1.0
    f["mom_20"] = hist_px.iloc[-1] / hist_px.iloc[-21] - 1.0
    f["mom_60"] = hist_px.iloc[-1] / hist_px.iloc[-61] - 1.0
    f["vol_20"] = hist_ret.tail(20).std(ddof=1)
    f["vol_60"] = hist_ret.tail(60).std(ddof=1)
    f["max_ret_20"] = hist_ret.tail(20).max()
    amt20 = hist_amt.tail(20).mean()
    f["log_amt_20"] = np.log(amt20.where(amt20 > 0))
    illiq = (hist_ret.tail(20).abs() / hist_amt.tail(20)).mean()
    f["illiq_20"] = np.log(illiq.where(illiq > 0))

    return f[f.notna().all(axis=1)]


# ------------------------------------------------------------------ 统计
def spearman(a: pd.Series, b: pd.Series) -> float:
    m = a.notna() & b.notna()
    if m.sum() < 30:
        return float("nan")
    return float(a[m].rank().corr(b[m].rank()))


def bucket_returns(fac: pd.Series, fwd: pd.Series, n: int) -> list[float]:
    """把 fac 分成 n 档（1=最小），返回各档月均收益。样本不足返回空。"""
    m = fac.notna() & fwd.notna()
    if m.sum() < n * 20:
        return []
    q = pd.qcut(fac[m].rank(method="first"), n, labels=False)
    grp = fwd[m].groupby(q).mean()
    if len(grp) < n:
        return []
    return [float(grp.get(i, np.nan)) for i in range(n)]


def industry_neutral(fac: pd.Series, ind: pd.Series) -> pd.Series:
    """减去同行业中位数。缺失行业的标的不参与（返回 NaN）。"""
    g = ind.reindex(fac.index)
    tmp = pd.DataFrame({"v": fac.astype(float), "g": g})
    ok = tmp["g"].notna() & tmp["v"].notna()
    med = tmp[ok].groupby("g")["v"].transform("median")
    out = tmp["v"] - med
    return out.where(ok)


def ann(monthly: float) -> float:
    return (1.0 + monthly) ** 12 - 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/processed")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2024-12-31")
    args = ap.parse_args()

    px, ret, amt = load_matrices(Path(args.data_dir))
    ind = None
    inst_path = Path(args.data_dir) / "instruments.parquet"
    if inst_path.exists():
        inst = pd.read_parquet(inst_path, columns=["symbol", "industry"])
        if "industry" in inst.columns and inst["industry"].notna().any():
            ind = inst.set_index("symbol")["industry"].astype(str)
            print(f"行业映射 {ind.notna().sum()} 只，{ind.nunique()} 个行业", flush=True)
    dates = px.index[(px.index >= pd.Timestamp(args.start)) & (px.index <= pd.Timestamp(args.end))]
    month_end = pd.Series(1, index=dates).resample("ME").last().dropna().index
    signal = pd.DatetimeIndex([px.index[px.index <= d][-1] for d in month_end])
    print(f"矩阵 {px.shape[0]} 日 x {px.shape[1]} 只，信号日 {len(signal)} 个", flush=True)

    ics: dict[str, list[float]] = {k: [] for k in FACTORS}
    ics_neu: dict[str, list[float]] = {k: [] for k in FACTORS}
    spreads: dict[str, list[float]] = {k: [] for k in FACTORS}
    # 每个流动性三档内：top 分位、bottom 分位、该档等权
    lay: dict[str, list[list[tuple[float, float, float]]]] = {k: [] for k in FACTORS}
    used = 0

    for i in range(len(signal) - 1):
        t, t_next = signal[i], signal[i + 1]
        fac = factor_panel(px, ret, amt, t)
        if fac.empty:
            continue
        fwd = (px.loc[t_next] / px.loc[t] - 1.0).reindex(fac.index)
        if fwd.notna().sum() < 300:
            continue
        used += 1

        # 流动性三档（用当日成交额，不含未来信息）
        tiers = pd.qcut(fac["log_amt_20"].rank(method="first"), 3, labels=False)

        for name in FACTORS:
            ics[name].append(spearman(fac[name], fwd))
            if ind is not None:
                v_neu = industry_neutral(fac[name], ind)
                ics_neu[name].append(spearman(v_neu, fwd))
            b = bucket_returns(fac[name], fwd, 10)
            if b:
                spreads[name].append(b[-1] - b[0])

            row = []
            for k in range(3):
                sel = tiers == k
                bb = bucket_returns(fac[name][sel], fwd[sel], 5)
                if len(bb) == 5:
                    row.append((bb[-1], bb[0], float(fwd[sel].mean())))
                else:
                    row.append((np.nan, np.nan, np.nan))
            lay[name].append(row)

    print(f"有效区间 {used} 个\n")

    print("表 1：原始因子值的预测力（IC 与十分档多空，同号才算单调）")
    print(f"{'因子':12s} {'IC均值':>8s} {'ICIR':>7s} {'十分档多空(年化)':>16s} {'行业中性IC':>10s} {'中性ICIR':>8s}")
    print("-" * 70)
    for name in FACTORS:
        a = np.array([x for x in ics[name] if not np.isnan(x)])
        s = np.array([x for x in spreads[name] if not np.isnan(x)])
        if not len(a):
            continue
        icir = a.mean() / a.std(ddof=1) if a.std(ddof=1) else float("nan")
        na = np.array([x for x in ics_neu[name] if not np.isnan(x)])
        if len(na):
            n_ic, n_icir = na.mean(), (na.mean() / na.std(ddof=1) if na.std(ddof=1) else float("nan"))
        else:
            n_ic = n_icir = float("nan")
        print(f"{name:12s} {a.mean():8.4f} {icir:7.3f} {ann(s.mean()):16.2%} "
              f"{n_ic:10.4f} {n_icir:8.3f}")

    print("\n表 2：流动性三档的等权绝对收益（长仓的对照基准）")
    print(f"{'档位':10s} {'年化':>9s}")
    print("-" * 22)
    for k, label in enumerate(("低流动 1/3", "中 1/3", "高流动 1/3")):
        vals = [r[k][2] for r in lay["vol_60"] if len(r) == 3 and not np.isnan(r[k][2])]
        print(f"{label:10s} {ann(np.mean(vals)):9.2%}")

    print("\n表 3：各因子在各流动性档内的长仓收益（5 档里的极端档，年化）")
    print(f"{'因子':12s} {'档位':12s} {'因子高5档':>10s} {'因子低5档':>10s} {'档内等权':>9s} {'高5档-等权':>11s}")
    print("-" * 74)
    for name in FACTORS:
        for k, label in enumerate(("低流动", "中流动", "高流动")):
            rows = [r[k] for r in lay[name] if len(r) == 3 and not np.isnan(r[k][0])]
            if not rows:
                continue
            top, bot, ew = (ann(np.mean([x[j] for x in rows])) for j in range(3))
            print(f"{name:12s} {label:12s} {top:10.2%} {bot:10.2%} {ew:9.2%} "
                  f"{top - ew:11.2%}")
        print()

    print("读法：只有『高5档 - 档内等权』在多个流动性档都显著为正，才算真因子；")
    print("      若只在低流动档为正，说明它在吃流动性溢价，规模上去就消失。")


if __name__ == "__main__":
    main()
