"""用 N 只股票复制"全宇宙等权"：跟踪误差与固定成本。

为什么要做：三段检验下来没有一个选股策略能稳定跑赢"宇宙等权"。
如果最终答案就是"持有整个宇宙等权"，那实盘必须回答两个问题——
1) 用 N 只抽样，和宇宙等权的偏差有多大（跟踪误差）；
2) 100 万本金下，N 只股票的最低佣金固定成本是多少。

方法：每月从宇宙里随机抽 N 只等权持有，与宇宙等权比较月度主动收益，
200 次抽样取分布。

用法：python scripts/replicate_universe.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"D:/a_share_quant")
TRADING_DAYS = 252
CAPITAL = 1_000_000
COMMISSION_MIN = 5.0          # 单笔最低佣金（元）
NS = [50, 100, 200, 400]

PERIODS = [
    ("2020-2024", ROOT / "data" / "processed"),
    ("2015-2019", ROOT / "data" / "processed_2015_2019"),
    ("2025-2026", ROOT / "data" / "processed_2025_2026"),
]


def monthly_panel(d: Path, min_hist: int = 60) -> pd.DataFrame:
    b = pd.read_parquet(d / "bars.parquet")
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b[(b["is_suspended"] == False) & b["adj_close"].notna()
          & (b["adj_close"] > 0) & (~b["is_st"].fillna(False))]
    b = b.sort_values(["symbol", "trade_date"])
    b["ret"] = b.groupby("symbol")["adj_close"].pct_change()
    b = b.dropna(subset=["ret"])
    px = b.pivot_table(index="trade_date", columns="symbol", values="adj_close")
    hist = px.notna().cumsum()
    ok = (hist >= min_hist).stack()
    ok.index.names = ["trade_date", "symbol"]
    b = b.set_index(["trade_date", "symbol"])
    b = b[b.index.isin(ok[ok].index)].reset_index()
    b["ym"] = b["trade_date"].dt.to_period("M")
    m = (1 + b["ret"]).groupby([b["symbol"], b["ym"]]).prod() - 1
    return m.unstack(level=0)


def main() -> None:
    rng = np.random.default_rng(20261021)
    print("固定成本：最低佣金 %.0f 元/笔，按每月全换手（双边各一次）估算\n" % COMMISSION_MIN)

    print(f"{'N':>5s} {'每只金额':>10s} {'单边佣金占比':>12s} {'年化成本(全换手)':>16s}")
    for n in NS:
        per = CAPITAL / n
        c = max(COMMISSION_MIN, per * 0.00025) / per
        print(f"{n:5d} {per:10,.0f} {c:12.3%} {c*2*12:16.2%}")
    print()

    for tag, d in PERIODS:
        M = monthly_panel(d)
        uni = M.mean(axis=1)
        n_months = uni.notna().sum()
        print(f"=== {tag}  ({n_months} 个月，全宇宙 {M.shape[1]} 只)")
        print(f"{'N':>5s} {'年化主动收益均值':>16s} {'跟踪误差(年化)':>15s} "
              f"{'5年累计偏离 5%~95%':>24s}")
        for n in NS:
            if n >= M.shape[1]:
                continue
            act, tot = [], []
            for _ in range(200):
                cols = rng.choice(M.shape[1], size=n, replace=False)
                s = M.iloc[:, cols].mean(axis=1)
                diff = (s - uni).dropna()
                act.append(diff)
                tot.append(((1 + s.fillna(0)).prod()) / ((1 + uni.fillna(0)).prod()) - 1)
            A = pd.concat(act, axis=1)
            # 每个抽样序列的年化主动收益标准差
            te = A.apply(lambda c: c.std(ddof=1)).mean() * np.sqrt(12)
            mean_act = (A.apply(lambda c: (1 + c).prod() ** (12 / len(c)) - 1)).mean()
            lo, hi = np.percentile(tot, 5), np.percentile(tot, 95)
            print(f"{n:5d} {mean_act:16.2%} {te:15.2%} {lo:11.1%} ~ {hi:6.1%}")
        print()

    print("读法：N 越大跟踪误差越小，但 100 万本金下每只金额越小、固定佣金占比越高。")
    print("      两者交叉决定实盘可用的 N。")


if __name__ == "__main__":
    main()
