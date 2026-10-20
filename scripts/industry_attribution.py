"""行业归因：低波组合的收益能不能被"行业组合"直接解释。

方法：用同一份行情算出各行业的等权月度收益，再用低波组合的平均行业暴露
加权，得到"如果只是行业结构，应该赚多少"。若这个数接近实际收益，
说明低波策略没有行业内的选股能力，它只是"买银行+公用事业"。

局限：用**平均**暴露而不是逐日暴露，是近似；行业分类是当前分类（非历史快照）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"D:/a_share_quant")


def load(min_history: int = 60) -> tuple[pd.DataFrame, pd.DataFrame]:
    b = pd.read_parquet(ROOT / "data" / "processed" / "bars.parquet")
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b[(b["is_suspended"] == False) & b["adj_close"].notna() & (b["adj_close"] > 0)]
    b = b.sort_values(["symbol", "trade_date"])
    b["ret"] = b.groupby("symbol")["adj_close"].pct_change()
    b = b.dropna(subset=["ret"])

    # 与引擎的股票池口径对齐：上市未满 min_history 个交易日的次新股不参与。
    # 不做这一步，行业等权会被 2015 年次新股的连板收益拉高（当年全宇宙等权
    # +126%，剔除次新后只有 +24%），归因差额会变成假的 -16pp。
    px = b.pivot_table(index="trade_date", columns="symbol", values="adj_close")
    hist = px.notna().cumsum()
    ok = (hist >= min_history).stack()
    ok.index.names = ["trade_date", "symbol"]
    keep = ok[ok].index
    # 用 isin 而不是 .loc：dropna 之后首行收益为 NaN 已被剔除，
    # 直接用 .loc 会抛 KeyError（那些 (date, symbol) 不在索引里）。
    b = b.set_index(["trade_date", "symbol"])
    b = b[b.index.isin(keep)].reset_index()
    return b, pd.read_parquet(
        ROOT / "data" / "processed" / "instruments.parquet", columns=["symbol", "industry"])


def industry_returns(ret: pd.DataFrame, ind: pd.Series) -> pd.DataFrame:
    """每个行业的等权**月度**收益（date x industry）。

    为什么必须月频：回测是月度调仓。若用"日频等权再按月复利"，会在高离散年份
    白拿"再平衡红利"——2015 年全宇宙日频等权 +27%，月频只有 +18%，
    差 9pp 全进了假 alpha。归因必须和回测同一调仓频率。
    """
    r = ret.copy()
    r["industry"] = r["symbol"].map(ind)
    r = r[r["industry"].notna()]
    r["ym"] = r["trade_date"].dt.to_period("M")
    # 个股月收益 -> 行业内横截面等权
    m = (1 + r["ret"]).groupby([r["symbol"], r["industry"], r["ym"]]).prod() - 1
    out = m.groupby(level=[1, 2]).mean().unstack(level=0)
    out.index = out.index.to_timestamp("M")
    return out


def annualize(s: pd.Series) -> float:
    s = s.dropna()
    if s.empty:
        return float("nan")
    return float((1 + s).prod() ** (12 / len(s)) - 1)


def main() -> None:
    exp_dir = None
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    min_hist = 60
    for a in sys.argv[1:]:
        if a.startswith("--min-history="):
            min_hist = int(a.split("=")[1])
    if args:
        exp_dir = args[0]
    ret, inst = load(min_hist)
    ind = inst.set_index("symbol")["industry"].astype(str)
    iret = industry_returns(ret, ind)
    print(f"行业 {iret.shape[1]} 个，{len(iret)} 个月\n")

    key = ["J66货币金融服务", "D44电力、热力生产和供应业", "G54道路运输业",
           "G55水上运输业", "D46水的生产和供应业"]
    print("关键行业等权年化（2020-2024）：")
    for k in key:
        if k in iret.columns:
            print(f"  {k[:24]:26s} {annualize(iret[k]):8.2%}")

    # 全池等权
    all_ret = ret.pivot_table(index="trade_date", columns="symbol", values="ret").mean(axis=1)
    all_m = (1 + all_ret).resample("ME").prod() - 1
    print(f"\n全池等权年化      {annualize(all_m):8.2%}")

    if exp_dir is None:
        return
    r = json.loads((Path(exp_dir) / "metrics.json").read_text(encoding="utf-8"))
    expo = r.get("industry_exposure_avg") or {}
    if not expo:
        print("\n该实验没有行业暴露数据")
        return
    total = sum(expo.values())
    print(f"\n组合行业暴露合计 {total:.1%}（其余为未匹配行业的标的）")

    matched, contrib = 0.0, 0.0
    for k, w in sorted(expo.items(), key=lambda kv: -kv[1]):
        if k not in iret.columns:
            continue
        a = annualize(iret[k])
        matched += w
        contrib += w * a
    print(f"\n行业归因：可匹配权重 {matched:.1%}，加权行业收益 {contrib:.2%}/年")
    print(f"组合实际年化 {r['annualized_return']:.2%}")
    print(f"差额（行业内选股/权重差异）{r['annualized_return'] - contrib:+.2%}")


if __name__ == "__main__":
    main()
