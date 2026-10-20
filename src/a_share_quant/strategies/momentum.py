"""动量选股策略：按月从股票池里挑动量最强的一批等权持有。

定位：这是**第一个有明确学术预期的策略**，用来检验框架能否区分
"因子收益"和"风格收益"——所以必须配合中证1000 之类的同规模基准看。

防未来函数：
  * 只用 ``trade_date <= as_of`` 的行情（引擎已保证，这里再截一次）；
  * 动量计算**跳过最近 ``skip`` 个交易日**（12-1 动量的标准做法），
    避免把最近一个月的短期反转算进信号。
"""
from __future__ import annotations

from typing import Sequence

import pandas as pd

from ..factors import Momentum, pivot_column
from .base import Strategy, TargetPortfolio


class TopNMomentum(Strategy):
    """动量前 N 名等权。

    参数
    ----
    top_n    持仓数量
    window   动量回看窗口（交易日）
    skip     跳过最近多少个交易日（默认 5，约一周）
    """

    name = "topn_momentum"

    def __init__(self, top_n: int = 50, window: int = 60, skip: int = 5,
                 reverse: bool = False) -> None:
        if top_n < 1:
            raise ValueError("top_n must be >= 1")
        if window < 1:
            raise ValueError("window must be >= 1")
        if skip < 0:
            raise ValueError("skip must be >= 0")
        self.top_n = top_n
        self.window = window
        self.skip = skip
        # reverse=True 选动量最弱的一批（A 股动量常为负，反向即通常说的"反转"）
        self.reverse = reverse
        self.warmup = window + skip + 1

    def generate_target(self, as_of, bars: pd.DataFrame,
                        universe: Sequence[str]) -> TargetPortfolio:
        ts = pd.Timestamp(as_of).normalize()
        pool = {str(s) for s in universe}
        if not pool or bars is None or bars.empty:
            return TargetPortfolio(as_of=ts, weights={})

        sub = bars[bars["symbol"].isin(pool)]
        if sub.empty:
            return TargetPortfolio(as_of=ts, weights={})
        sub = sub[sub["trade_date"] <= ts]

        col = "adj_close" if "adj_close" in sub.columns else "close"
        px = pivot_column(sub, col).sort_index()
        need = self.window + self.skip + 1
        if len(px) < need:
            return TargetPortfolio(as_of=ts, weights={})

        # 12-1 动量：从 t-skip 往回看 window 天（不含最近 skip 天）
        end = px.iloc[-1 - self.skip]
        start = px.iloc[-1 - self.skip - self.window]
        mom = (end / start - 1.0).replace([float("inf"), float("-inf")], pd.NA)
        mom = pd.to_numeric(mom, errors="coerce").dropna()
        if mom.empty:
            return TargetPortfolio(as_of=ts, weights={})

        picked = mom.sort_values(ascending=self.reverse).head(self.top_n).index
        w = 1.0 / len(picked)
        return TargetPortfolio(as_of=ts, weights={str(s): w for s in picked})
