"""示例因子：简单、透明、易于手工验证。

第一阶段刻意不实现复杂因子；每个因子的数学定义、数据输入、
信号形成时间、复权方式都在 docstring 中明确。
"""
from __future__ import annotations

import pandas as pd

from .base import Factor, pivot_column


class Momentum(Factor):
    """N 日动量。

    数学定义：mom(t) = P(t) / P(t - N) - 1
    数据输入：后复权收盘价 adj_close（uses_adjusted=True）
    信号形成时间：t 日收盘（只能用于 t+1 及以后的交易）
    暖机期：N 个交易日
    """

    uses_adjusted = True

    def __init__(self, window: int = 20) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window = window
        self.name = f"momentum_{window}"

    def compute(self, bars: pd.DataFrame) -> pd.DataFrame:
        px = pivot_column(bars, "adj_close" if self.uses_adjusted else "close")
        return px / px.shift(self.window) - 1.0


class Volatility(Factor):
    """N 日波动率。

    数学定义：日简单收益率 r(t) = P(t)/P(t-1) - 1 在窗口内的样本标准差
    数据输入：后复权收盘价 adj_close
    信号形成时间：t 日收盘；暖机期：N+1 个交易日
    """

    uses_adjusted = True

    def __init__(self, window: int = 20) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window = window
        self.name = f"volatility_{window}"

    def compute(self, bars: pd.DataFrame) -> pd.DataFrame:
        px = pivot_column(bars, "adj_close" if self.uses_adjusted else "close")
        ret = px / px.shift(1) - 1.0
        return ret.rolling(self.window).std(ddof=1)


class AvgAmount(Factor):
    """N 日成交额均值（流动性因子）。

    数学定义：amt(t) = mean(amount(t-N+1 .. t))
    数据输入：原始成交额 amount（不复权——成交额本身无复权概念）
    信号形成时间：t 日收盘；暖机期：N 个交易日
    """

    uses_adjusted = False

    def __init__(self, window: int = 20) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window = window
        self.name = f"avg_amount_{window}"

    def compute(self, bars: pd.DataFrame) -> pd.DataFrame:
        amt = pivot_column(bars, "amount")
        return amt.rolling(self.window).mean()
