"""目标组合构建：权重清洗（数量上限、单票上限、残差现金）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PortfolioRules:
    """组合约束。"""

    max_positions: int = 20            # 最大持仓数量
    max_weight_per_stock: float = 0.10  # 单只股票最大权重


def build_target_weights(raw: Mapping[str, float],
                         rules: PortfolioRules) -> dict[str, float]:
    """清洗目标权重：截断数量 -> 单票上限 -> 被裁掉的权重保留为现金。

    保持 sum(weights) <= 1，不强行归一到满仓，避免“为满仓而满仓”
    隐式放大风险。截断顺序：权重降序（同权重按字典序）。
    """
    if rules.max_positions < 1:
        raise ValueError("max_positions must be >= 1")
    if not 0 < rules.max_weight_per_stock <= 1:
        raise ValueError("max_weight_per_stock must be in (0, 1]")

    items = sorted(((str(s), float(w)) for s, w in dict(raw or {}).items() if float(w) > 0),
                   key=lambda x: (-x[1], x[0]))
    items = items[: rules.max_positions]

    out: dict[str, float] = {}
    remaining = 1.0
    for sym, w in items:
        take = min(w, rules.max_weight_per_stock, remaining)
        if take <= 0:
            break
        out[sym] = take
        remaining -= take
    return out
