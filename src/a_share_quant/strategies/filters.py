"""选股前的公共过滤：只保留成交额足够大的一批。

为什么需要它：本项目已确认流动性/规模溢价极大——全宇宙等权 8.72%/年，
而成交额最低的 1/3 是 24.1%、最高的 1/3 只有 0.3%（2020-2024）。
所以任何策略都必须能在**同一流动性档**里跟等权对照，否则分不清
"因子有效"和"吃到了流动性溢价"。两个策略共用本模块，保证口径一致。
"""
from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd

from ..factors import pivot_column


def liquid_symbols(bars: pd.DataFrame, as_of, pool: Iterable[str],
                   liquidity_pct: Optional[float],
                   amount_window: int = 20) -> Optional[set[str]]:
    """返回成交额最高的前 ``liquidity_pct`` 比例内的代码集合。

    ``liquidity_pct`` 为 None 时返回 None（表示不限制）。
    只用 ``trade_date <= as_of`` 的成交额，不含未来信息。
    """
    if liquidity_pct is None:
        return None
    if not 0 < liquidity_pct <= 1:
        raise ValueError("liquidity_pct must be in (0, 1]")

    ts = pd.Timestamp(as_of).normalize()
    sub = bars[bars["symbol"].isin({str(s) for s in pool})]
    sub = sub[sub["trade_date"] <= ts]
    if sub.empty or "amount" not in sub.columns:
        return set()

    amt = pivot_column(sub, "amount").sort_index()
    avg = amt.tail(amount_window).mean().dropna()
    if avg.empty:
        return set()
    cutoff = avg.quantile(1.0 - liquidity_pct)
    return {str(s) for s in avg[avg >= cutoff].index}


def capacity_symbols(bars: pd.DataFrame, as_of, pool: Iterable[str],
                     aum: float, participation_rate: float,
                     target_weight: float,
                     amount_window: int = 20) -> set[str]:
    """只保留"装得下目标权重"的标的。

    约束：``participation_rate × 20 日均成交额 >= target_weight × AUM``。
    即"按目标权重买入这一只，不会超过当日成交量的 x%"。

    为什么要在选股阶段做：引擎只在撮合时拦截超量订单，那时已经选完股了，
    结果就是"选出来一堆买不满的标的"，权重被动偏离、现金闲置。
    把约束前移到选股，等于先问"这笔钱进得去吗"。

    只在 ``aum`` 明确给出时启用；100 万本金下这个约束几乎不生效，
    真正有意义的是放大到几千万以上时的压力测试。
    """
    if aum <= 0:
        raise ValueError("aum must be > 0")
    if not 0 < participation_rate <= 1:
        raise ValueError("participation_rate must be in (0, 1]")

    ts = pd.Timestamp(as_of).normalize()
    sub = bars[bars["symbol"].isin({str(s) for s in pool})]
    sub = sub[sub["trade_date"] <= ts]
    if sub.empty or "amount" not in sub.columns:
        return set()

    amt = pivot_column(sub, "amount").sort_index()
    avg = amt.tail(amount_window).mean().dropna()
    if avg.empty:
        return set()
    need = target_weight * aum / participation_rate
    return {str(s) for s in avg[avg >= need].index}
