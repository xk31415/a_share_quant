"""策略模块。"""
from .base import Strategy, TargetPortfolio
from .equal_weight import EqualWeightStrategy

__all__ = ["Strategy", "TargetPortfolio", "EqualWeightStrategy"]
