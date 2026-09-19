"""因子基类与工具。

因子分类（可扩展）：
    Factor
    ├── Momentum      动量
    ├── Reversal      反转（待实现）
    ├── Value         价值（待实现，需财务数据）
    ├── Quality       质量（待实现，需财务数据）
    ├── Volatility    波动率
    └── Liquidity     流动性
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


def pivot_column(bars: pd.DataFrame, column: str) -> pd.DataFrame:
    """把 long-format 行情透视为 (trade_date × symbol) 面板。"""
    if column not in bars.columns:
        raise KeyError(f"bars missing column {column!r} required by factor")
    return bars.pivot(index="trade_date", columns="symbol", values=column).sort_index()


class Factor(ABC):
    """因子基类。

    契约（必须遵守）：
      * compute(bars) 输出在日期 t 的值只允许使用 trade_date <= t 的行；
        调用方负责截断输入，因子实现不得读取任何未来行；
      * 输出为 (trade_date × symbol) 的 DataFrame，缺失值用 NaN；
      * name/window/uses_adjusted 必须明确声明，用于审计与暖机期判断。
    """

    name: str = "factor"
    window: int = 1            # 计算所需的最小历史 bar 数（暖机期）
    uses_adjusted: bool = True  # 是否基于后复权价格

    @abstractmethod
    def compute(self, bars: pd.DataFrame) -> pd.DataFrame:
        """计算因子面板 (trade_date × symbol)。"""

    def compute_as_of(self, bars: pd.DataFrame, as_of) -> pd.Series:
        """只用 <= as_of 的数据，返回 as_of 当日横截面。"""
        ts = pd.Timestamp(as_of).normalize()
        panel_input = bars.loc[pd.to_datetime(bars["trade_date"]) <= ts]
        if panel_input.empty:
            return pd.Series(dtype=float)
        panel = self.compute(panel_input)
        if ts in panel.index:
            return panel.loc[ts]
        return pd.Series(dtype=float)
