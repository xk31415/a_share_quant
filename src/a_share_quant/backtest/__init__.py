"""回测模块：引擎、执行模拟、成本、持仓。"""
from .costs import CostModel
from .engine import BacktestConfig, BacktestEngine, BacktestResult
from .execution import ExecutionSimulator, Fill, Order, generate_orders
from .positions import (
    InsufficientCash,
    InsufficientShares,
    PortfolioState,
    Position,
)

__all__ = [
    "CostModel",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "ExecutionSimulator",
    "Order",
    "Fill",
    "generate_orders",
    "PortfolioState",
    "Position",
    "InsufficientShares",
    "InsufficientCash",
]
