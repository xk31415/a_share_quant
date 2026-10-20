"""容量测算：在 max_participation_rate 约束下，这套策略最多能管多少钱。

方法：回测是在 100 万本金下跑的，每笔成交股数 S 与成交日成交量 V 已知。
把本金放大 k 倍，成交股数近似放大 k 倍，于是"成交不超过当日成交量 p 比例"
这一约束给出的上限是：

    k_max = p * V / S          ->  AUM_max = 初始资金 * k_max

对每笔成交算 k_max，取分位数。**最小值就是"一笔都别想超"的硬上限**，
5% 分位数更接近实际可操作的容量（个别极端小的成交不该定义整体容量）。

局限（必须说清，而且第一条实测证明了这个估算**偏高一个量级**）：
* 这是"按已成交的股数线性缩放"的近似，假设放大本金后仍然持有同一批股票。
  实际放大后，原来那些成交对应的小票**根本选不进来**，选股集合会整体上移。
  实测（启用容量约束重跑）：等权 50 只在 1 亿 7.05%、5 亿 -1.30%、10 亿 -3.23%，
  而本脚本给出的是"5% 分位 5.4 亿"——**真实上限约 1 亿**。
  所以本脚本只适合当"乐观上限"，结论请以 `--capacity-aum` 的实际重跑为准；
* 只用成交日的全日成交量，而撮合发生在开盘——开盘可成交量通常远小于全日量，
  所以还要再打折（见 README 限制清单）；
* 不含冲击成本随规模上升的部分。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True, help="results/EXP-xxxx 目录")
    ap.add_argument("--data-dir", default="data/processed")
    ap.add_argument("--rate", type=float, default=0.10, help="单笔成交量占比上限")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    args = ap.parse_args()

    exp = Path(args.exp)
    trades = pd.read_csv(exp / "trades.csv", dtype={"symbol": str})
    trades["trade_date"] = pd.to_datetime(trades["trade_date"])
    filled = trades[(trades["note"] == "filled") & (trades["shares"] > 0)].copy()

    bars = pd.read_parquet(Path(args.data_dir) / "bars.parquet",
                           columns=["trade_date", "symbol", "volume"])
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])

    m = filled.merge(bars, on=["trade_date", "symbol"], how="left")
    missing = int(m["volume"].isna().sum())
    m = m[m["volume"].notna() & (m["volume"] > 0)]
    print(f"成交 {len(filled)} 笔，匹配到成交量 {len(m)} 笔，缺失 {missing} 笔")

    m["k_max"] = args.rate * m["volume"] / m["shares"]
    m["aum_max"] = args.capital * m["k_max"]

    qs = [0.01, 0.05, 0.10, 0.25, 0.50]
    print(f"\n隐含可管理规模（本金 {args.capital:,.0f}，占比上限 {args.rate:.0%}）：")
    print(f"  硬上限（最小值）   {m['aum_max'].min():>18,.0f}")
    for q in qs:
        print(f"  {int(q*100):>2d}% 分位          {m['aum_max'].quantile(q):>18,.0f}")

    # 按金额口径再看一遍：小单（金额小）容易被"整手 + 最低佣金"卡住，
    # 大单才是容量瓶颈。用成交金额排序，看最贵的那批。
    m["turnover"] = m["shares"] * m["price"]
    big = m.nlargest(min(50, len(m)), "turnover")
    print(f"\n最大 50 笔成交的平均金额 {big['turnover'].mean():,.0f} 元，"
          f"其隐含容量 5% 分位 {big['aum_max'].quantile(0.05):,.0f}")

    # 分年看：容量是否随时间恶化（小盘流动性收缩）
    m["year"] = m["trade_date"].dt.year
    print("\n分年（5% 分位 / 硬上限）：")
    for y, g in m.groupby("year"):
        print(f"  {y}  5% 分位 {g['aum_max'].quantile(0.05):>15,.0f}   "
              f"最小 {g['aum_max'].min():>15,.0f}   成交 {len(g)} 笔")


if __name__ == "__main__":
    main()
