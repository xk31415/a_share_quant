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
    """日频等权再平衡的月度复合收益（含日频再平衡红利，仅作参考）。"""
    b = pd.read_parquet(data_dir / "bars.parquet")
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b[(b["is_suspended"] == False) & b["adj_close"].notna() & (b["adj_close"] > 0)]
    b = b.sort_values(["symbol", "trade_date"])
    b["ret"] = b.groupby("symbol")["adj_close"].pct_change()
    b = b.dropna(subset=["ret"])
    mat = b.pivot_table(index="trade_date", columns="symbol", values="ret")
    daily = mat.mean(axis=1)
    return (1 + daily).resample("ME").prod() - 1


def monthly_ew_returns(data_dir: Path, min_hist: int = 60) -> pd.Series:
    """月频等权收益（与 replicate_universe.py 同源：个股月内买入持有，月末等权）。

    注意：必须在全量 bars 上先算 ``cumsum() >= min_hist``（暖机），再按月份切片；
    若先把 bars 截到 ``>= 04-30`` 再算 ``cumsum() >= 60``，会造成**双重暖机截断**，
    把 5~7 月的已建仓月份误删（历史坑：2020-2024 因此从 +9.85% 被误算成 +4.95%）。
    """
    b = pd.read_parquet(data_dir / "bars.parquet")
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b[(b["is_suspended"] == False) & b["adj_close"].notna()
          & (b["adj_close"] > 0) & (~b["is_st"].fillna(False))]
    b = b.sort_values(["symbol", "trade_date"])
    b["ret"] = b.groupby("symbol")["adj_close"].pct_change()
    b = b.dropna(subset=["ret"])
    px = b.pivot_table(index="trade_date", columns="symbol", values="adj_close")
    ok = (px.notna().cumsum() >= min_hist).stack()
    ok.index.names = ["trade_date", "symbol"]
    b = b.set_index(["trade_date", "symbol"])
    b = b[b.index.isin(ok[ok].index)].reset_index()
    b["ym"] = b["trade_date"].dt.to_period("M")
    m = (1 + b["ret"]).groupby([b["symbol"], b["ym"]]).prod() - 1
    return m.unstack(level=0).mean(axis=1)


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
    for tag, d, first_ym in (
        ("2020-2024", ROOT / "data" / "processed", "2020-05"),
        ("2015-2019", ROOT / "data" / "processed_2015_2019", "2015-05"),
        ("2025-2026", ROOT / "data" / "processed_2025_2026", "2025-05"),
    ):
        if not (d / "bars.parquet").exists():
            continue
        print(f"=== {tag}  ({d.name})")
        m_ew = monthly_ew_returns(d)
        summarize("月频宇宙等权(全区间)", m_ew)
        summarize("月频宇宙等权(5月起)", m_ew.loc[m_ew.index >= pd.Period(first_ym, freq="M")])
        summarize("日频宇宙等权(参考)", ew_returns(d))
        summarize("中证1000(5月起)", bench_returns(d).loc[lambda s: s.index >= pd.Timestamp(f"{first_ym}-01")])
        print()


if __name__ == "__main__":
    main()
