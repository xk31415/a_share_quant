"""交易成本模型（独立模块，禁止写死在策略里）。

A 股成本构成：
  * commission  券商佣金：双边收取，单笔最低 commission_min 元；
  * stamp_duty  印花税：仅卖出收取（2023-08-28 起税率 0.05%）；
  * slippage    滑点：按成交参考价的比例向不利方向偏移。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """A 股交易成本参数。"""

    commission_rate: float = 0.00025
    commission_min: float = 5.0
    stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.0005

    def __post_init__(self) -> None:
        for field in ("commission_rate", "stamp_duty_rate", "slippage_rate"):
            v = getattr(self, field)
            if not 0 <= v < 0.1:
                raise ValueError(f"{field} must be in [0, 0.1), got {v}")
        if self.commission_min < 0:
            raise ValueError("commission_min must be >= 0")

    # -- 滑点后的成交价 --
    def buy_price(self, ref_price: float) -> float:
        """买入成交价：参考价上浮滑点。"""
        return float(ref_price) * (1.0 + self.slippage_rate)

    def sell_price(self, ref_price: float) -> float:
        """卖出成交价：参考价下压滑点。"""
        return float(ref_price) * (1.0 - self.slippage_rate)

    # -- 成本 --
    def buy_cost(self, turnover: float) -> float:
        """买入成本：佣金（含最低佣金）。"""
        if turnover <= 0:
            return 0.0
        return max(turnover * self.commission_rate, self.commission_min)

    def sell_cost(self, turnover: float) -> float:
        """卖出成本：佣金 + 印花税。"""
        if turnover <= 0:
            return 0.0
        commission = max(turnover * self.commission_rate, self.commission_min)
        return commission + turnover * self.stamp_duty_rate
