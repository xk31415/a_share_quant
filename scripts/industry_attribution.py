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


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    b = pd.read_parquet(ROOT / "data" / "processed" / "bars.parquet")
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b[(b["is_suspended"] == False) & b["adj_close"].notna() & (b["adj_close"] > 0)]
    b = b.sort_values(["symbol", "trade_date"])
    b["ret"] = b.groupby("symbol")["adj_close"].pct_change()
    return b.dropna(subset=["ret"]), pd.read_parquet(
        ROOT / "data" / "processed" / "instruments.parquet", columns=["symbol", "industry"])


def industry_returns(ret: pd.DataFrame, ind: pd.Series) -> pd.DataFrame:
    """每个行业的等权月度收益（date x industry）。"""
    r = ret.copy()
    r["industry"] = r["symbol"].map(ind)
    r = r[r["industry"].notna()]
    mat = r.pivot_table(index="trade_date", columns="symbol", values="ret")
    cols = {}
    for name, g in r.groupby("industry"):
        syms = [s for s in g["symbol"].unique() if s in mat.columns]
        if len(syms) < 5:
            continue
        cols[name] = mat[syms].mean(axis=1)
    out = pd.DataFrame(cols)
    return out.resample("ME").apply(lambda x: (1 + x).prod() - 1).dropna(how="all")


def annualize(s: pd.Series) -> float:
    s = s.dropna()
    if s.empty:
        return float("nan")
    return float((1 + s).prod() ** (12 / len(s)) - 1)


def main() -> None:
    exp_dir = sys.argv[1] if len(sys.argv) > 1 else None
    ret, inst = load()
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
