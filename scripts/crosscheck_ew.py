"""样本外交叉验证：用纯收益口径算全宇宙等权，与回测引擎结果对照。

如果两条路给出一致的数字，说明 -12.7% 是市场本身；如果不一致，说明引擎或数据
有问题（例如退市处理、复权因子、涨跌停拦截）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"D:/a_share_quant")


def ew_returns(data_dir: Path) -> pd.Series:
    b = pd.read_parquet(data_dir / "bars.parquet")
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b[(b["is_suspended"] == False) & b["adj_close"].notna() & (b["adj_close"] > 0)]
    b = b.sort_values(["symbol", "trade_date"])
    b["ret"] = b.groupby("symbol")["adj_close"].pct_change()
    b = b.dropna(subset=["ret"])
    mat = b.pivot_table(index="trade_date", columns="symbol", values="ret")
    daily = mat.mean(axis=1)
    return (1 + daily).resample("ME").prod() - 1


def bench_returns(data_dir: Path, code: str = "000852") -> pd.Series:
    b = pd.read_csv(data_dir / "benchmark.csv", dtype={"symbol": str})
    b["trade_date"] = pd.to_datetime(b["trade_date"].astype(str).str.slice(0, 10))
    b = b[b["symbol"] == code].set_index("trade_date")["close"].sort_index()
    return (1 + b.pct_change().dropna()).resample("ME").prod() - 1


def summarize(name: str, s: pd.Series) -> None:
    s = s.dropna()
    n = len(s)
    ann = float((1 + s).prod() ** (12 / n) - 1)
    eq = (1 + s).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    by_year = (1 + s).groupby(s.index.year).prod() - 1
    print(f"{name:18s} 年化 {ann:7.2%}  区间累计 {float(eq.iloc[-1]-1):8.2%}  "
          f"最大回撤 {mdd:7.2%}")
    print("   分年: " + "  ".join(f"{y}:{v:+.1%}" for y, v in by_year.items()))


def main() -> None:
    for tag, d in (("2015-2019", ROOT / "data" / "processed_2015_2019"),
                   ("2020-2024", ROOT / "data" / "processed")):
        print(f"=== {tag}  ({d.name})")
        ew = ew_returns(d)
        summarize("全宇宙等权(纯算)", ew)
        summarize("中证1000", bench_returns(d))
        print()


if __name__ == "__main__":
    main()
