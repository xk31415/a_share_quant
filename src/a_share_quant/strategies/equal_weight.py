"""基准策略：股票池等权 + 定期调仓。

这是第一套“框架验证策略”：固定规则股票池 + 等权持仓 + 定期调仓。
目的只是测试数据读取、股票池、调仓、T+1、交易成本、净值与绩效计算，
不是研究的最终策略。
"""
from __future__ import annotations

from typing import Sequence

import pandas as pd

from .base import Strategy, TargetPortfolio


class EqualWeightStrategy(Strategy):
    """对当日股票池等权配置；超过 max_positions 时按字典序截断。

    说明：更合理的截断方式是按流动性/市值排序；此处刻意保持最简，
    因为该策略只为框架冒烟测试服务。
    """

    name = "equal_weight"

    def __init__(self, max_positions: int = 20) -> None:
        if max_positions < 1:
            raise ValueError("max_positions must be >= 1")
        self.max_positions = max_positions

    def generate_target(self, as_of, bars: pd.DataFrame,
                        universe: Sequence[str]) -> TargetPortfolio:
        picked = sorted(universe)[: self.max_positions]
        ts = pd.Timestamp(as_of).normalize()
        if not picked:
            return TargetPortfolio(as_of=ts, weights={})
        w = 1.0 / len(picked)
        return TargetPortfolio(as_of=ts, weights={s: w for s in picked})
