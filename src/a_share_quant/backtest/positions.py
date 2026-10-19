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

    adj_ref 是**建仓当日的复权因子**（``k = adj_close / close``），只用于估值：
    估值时按 ``k_t / adj_ref`` 把原始价折算成"建仓基准下的等价价格"。
    股数、成本、T+1、整手约束全部保持原始口径，只有估值这一步做还原——
    这样 10 送 10 除权日原始价腰斩不会让市值凭空少一半。
    """

    symbol: str
    shares: int = 0
    available_shares: int = 0
    avg_cost: float = 0.0  # 含费摊薄成本
    adj_ref: float = 1.0   # 建仓时点的复权因子 k = adj_close/close

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
    def buy(self, symbol: str, shares: int, price: float, cost: float,
            adj_factor: float = 1.0) -> None:
        """买入。``adj_factor`` 为当日复权因子，用于维护估值基准 ``adj_ref``。"""
        if shares <= 0:
            raise ValueError("shares must be positive")
        turnover = shares * price
        if turnover + cost > self.cash + 1e-6:
            raise InsufficientCash(
                f"need {turnover + cost:.2f}, cash {self.cash:.2f}")
        pos = self.positions.setdefault(symbol, Position(symbol))
        k = float(adj_factor) if adj_factor and adj_factor > 0 else 1.0
        if pos.shares <= 0:
            pos.adj_ref = k
        else:
            # 旧仓按当前 k 折算、新仓按 k 计价，再以市值加权求新的建仓基准。
            # 价格在两侧同时出现，可约掉，只剩股数与 k 的比值。
            old_weight = pos.shares * k / pos.adj_ref
            pos.adj_ref = (pos.shares + shares) * k / (old_weight + shares)
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
    def position_value(self, symbol: str, price: float,
                       adj_factor: float = 1.0) -> float:
        """单只持仓的市值（已按复权因子还原）。

        有公司行动时原始价会跳空（10 送 10 当天腰斩），但持仓的经济价值不变；
        ``k_t / adj_ref`` 把这个跳空抵掉。没有公司行动时 k ≡ 1，等价于
        ``股数 × 原始价``。
        """
        pos = self.positions.get(symbol)
        if pos is None or not pos.shares:
            return 0.0
        k = float(adj_factor) if adj_factor and adj_factor > 0 else 1.0
        ref = pos.adj_ref if pos.adj_ref > 0 else 1.0
        return pos.shares * float(price) * (k / ref)

    def market_value(self, prices: Mapping[str, float],
                     adj_factors: Mapping[str, float] | None = None) -> float:
        """总权益 = 现金 + Σ 持仓市值。缺失价格视为数据错误（显式报错）。"""
        k_map = adj_factors or {}
        total = self.cash
        for sym, pos in self.positions.items():
            if pos.shares:
                if sym not in prices:
                    raise ValueError(f"missing price for {sym}")
                total += self.position_value(sym, float(prices[sym]),
                                             k_map.get(sym, 1.0))
        return total

    def holdings(self) -> pd.DataFrame:
        rows = [
            {"symbol": p.symbol, "shares": p.shares,
             "available_shares": p.available_shares, "avg_cost": p.avg_cost}
            for p in sorted(self.positions.values(), key=lambda x: x.symbol)
        ]
        return pd.DataFrame(
            rows, columns=["symbol", "shares", "available_shares", "avg_cost"])
