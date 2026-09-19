"""因子框架：统一接口 + 防未来函数约定。"""
from .base import Factor, pivot_column
from .technical import AvgAmount, Momentum, Volatility

__all__ = ["Factor", "pivot_column", "Momentum", "Volatility", "AvgAmount"]
