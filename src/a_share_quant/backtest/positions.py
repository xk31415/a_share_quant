"""持仓与组合状态（含 T+1 可卖数量管理）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping

import pandas as pd


class InsufficientShares(ValueError):
    """尝试卖出超过可卖数量（T+1 限制或超出持仓）。"""


class InsufficientCash(ValueError):
    """现金不足以完成买入。"""


@dataclass
class Position:
    """单只股票持仓。

    available_shares 实现A股 T+1：当日买入的份额不计入 available_shares，
    次日开盘（start_of_day）才释放。
    """

    symbol: str
    shares: int = 0
    available_shares: int = 0
    avg_cost: float = 0.0  # 含费摊薄成本

    @property
    def locked_shares(self) -> int:
        return self.shares - self.available_shares


class PortfolioState:
    """现金 + 持仓；所有成交通过 buy/sell 落账，便于审计。"""

    def __init__(self, initial_cash: float) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        self.cash = float(initial_cash)
        self.initial_cash = float(initial_cash)
        self.positions: Dict[str, Position] = {}

    # ---- T+1 ----
    def start_of_day(self) -> None:
        """每个交易日开盘前调用：昨日及更早持有的份额今日可卖。"""
        for pos in self.positions.values():
            pos.available_shares = pos.shares

    # ---- 成交 ----
    def buy(self, symbol: str, shares: int, price: float, cost: float) -> None:
        if shares <= 0:
            raise ValueError("shares must be positive")
        turnover = shares * price
        if turnover + cost > self.cash + 1e-6:
            raise InsufficientCash(
                f"need {turnover + cost:.2f}, cash {self.cash:.2f}")
        pos = self.positions.setdefault(symbol, Position(symbol))
        total_cost = pos.avg_cost * pos.shares + turnover + cost
        pos.shares += shares
        pos.avg_cost = total_cost / pos.shares if pos.shares else 0.0
        # 当日买入不计入 available_shares（T+1）
        self.cash -= turnover + cost

    def sell(self, symbol: str, shares: int, price: float, cost: float) -> None:
        if shares <= 0:
            raise ValueError("shares must be positive")
        pos = self.positions.get(symbol)
        available = pos.available_shares if pos is not None else 0
        if available < shares:
            raise InsufficientShares(
                f"cannot sell {shares} of {symbol}: available {available}")
        self.cash += shares * price - cost
        pos.shares -= shares
        pos.available_shares -= shares
        if pos.shares == 0:
            del self.positions[symbol]

    # ---- 估值 ----
    def market_value(self, prices: Mapping[str, float]) -> float:
        """总权益 = 现金 + Σ 持仓 * 价格。缺失价格视为数据错误（显式报错）。"""
        total = self.cash
        for sym, pos in self.positions.items():
            if pos.shares:
                if sym not in prices:
                    raise ValueError(f"missing price for {sym}")
                total += pos.shares * float(prices[sym])
        return total

    def holdings(self) -> pd.DataFrame:
        rows = [
            {"symbol": p.symbol, "shares": p.shares,
             "available_shares": p.available_shares, "avg_cost": p.avg_cost}
            for p in sorted(self.positions.values(), key=lambda x: x.symbol)
        ]
        return pd.DataFrame(
            rows, columns=["symbol", "shares", "available_shares", "avg_cost"])
