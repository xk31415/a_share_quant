"""策略接口：只负责“数据 -> 目标权重”。

策略本身不负责（职责必须分离）：
  * 读取文件（数据由引擎注入）；
  * 计算手续费/印花税/滑点（costs 模块）；
  * 执行订单（execution 模块）；
  * 计算最终绩效（metrics 模块）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Sequence

import pandas as pd


@dataclass(frozen=True)
class TargetPortfolio:
    """t 日收盘生成的目标权重，将于 t+1 执行。

    weights: symbol -> 权重（0 < w <= 1）；不在 dict 中的现有持仓将被清仓。
    """

    as_of: pd.Timestamp
    weights: Mapping[str, float]


class Strategy(ABC):
    """策略基类。

    契约：
      * 只消费传入的截至 as_of 的数据（引擎保证 bars 的 trade_date <= as_of）；
      * 只产出目标权重；
      * 不做 I/O、不算成本、不下订单、不算绩效。

    warmup：策略内因子所需的最小历史交易日数（暖机期）。声明后引擎会在启动时
    校验窗口是否够长——不声明则暖机不足会静默产出空目标（全现金），
    这种失败模式在回测里几乎看不出来。
    """

    name: str = "strategy"
    warmup: int = 0

    @abstractmethod
    def generate_target(self, as_of, bars: pd.DataFrame,
                        universe: Sequence[str]) -> TargetPortfolio:
        """根据截至 as_of 的行情与当日股票池生成目标组合。

        参数：
            as_of: 信号日（t 日，收盘后）
            bars: 截至 as_of（含）的全部行情，long format
            universe: as_of 日可投资股票列表（已按时间点过滤）
        """
