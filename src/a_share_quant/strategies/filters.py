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
