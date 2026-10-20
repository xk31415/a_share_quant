"""低波动策略：按月从股票池里挑波动率最低的一批等权持有。

学术预期：低波异象（low-volatility anomaly）——低波股票的风险调整收益
长期高于高波股票。但 A 股 2020-2024 的实测显示，这个"异象"大部分时候
其实是**流动性溢价的投影**：低流动的 1/3 年化 24%，高流动的 1/3 只有 0.3%。
所以本策略提供 ``liquidity_pct`` 参数，可以在"成交额最高的前 x%"里选股，
配合同档位的等权对照，才能判断因子本身是否有用。

防未来函数：只用 ``trade_date <= as_of`` 的行情（引擎已保证，这里再截一次）；
成交额过滤用的是截至 as_of 的 20 日均额，不含未来信息。
"""
from __future__ import annotations

from typing import Optional, Sequence

import pandas as pd

from ..factors import pivot_column
from .base import Strategy, TargetPortfolio
from .filters import liquid_symbols


class LowVolatility(Strategy):
    """波动率最低的 N 只等权。

    参数
    ----
    top_n          持仓数量
    window         波动率回看窗口（交易日）
    liquidity_pct  只保留成交额最高的这个比例（0.33 = 前 1/3）；None = 不筛
    amount_window  成交额均值窗口（交易日）
    """

    name = "low_volatility"

    def __init__(self, top_n: int = 50, window: int = 60,
                 liquidity_pct: Optional[float] = None,
                 amount_window: int = 20) -> None:
        if top_n < 1:
            raise ValueError("top_n must be >= 1")
        if window < 2:
            raise ValueError("window must be >= 2")
        if liquidity_pct is not None and not 0 < liquidity_pct <= 1:
            raise ValueError("liquidity_pct must be in (0, 1]")
        self.top_n = top_n
        self.window = window
        self.liquidity_pct = liquidity_pct
        self.amount_window = amount_window
        self.warmup = max(window + 1, amount_window)

    def generate_target(self, as_of, bars: pd.DataFrame,
                        universe: Sequence[str]) -> TargetPortfolio:
        ts = pd.Timestamp(as_of).normalize()
        pool = {str(s) for s in universe}
        if not pool or bars is None or bars.empty:
            return TargetPortfolio(as_of=ts, weights={})

        sub = bars[bars["symbol"].isin(pool)]
        sub = sub[sub["trade_date"] <= ts]
        if sub.empty:
            return TargetPortfolio(as_of=ts, weights={})

        col = "adj_close" if "adj_close" in sub.columns else "close"
        px = pivot_column(sub, col).sort_index()
        if len(px) < self.window + 1:
            return TargetPortfolio(as_of=ts, weights={})

        ret = px.pct_change()
        vol = ret.tail(self.window).std(ddof=1).dropna()
        if vol.empty:
            return TargetPortfolio(as_of=ts, weights={})

        # 流动性过滤：只有成交额最高的前 liquidity_pct 才有资格入选
        if self.liquidity_pct is not None:
            allow = liquid_symbols(bars, ts, pool, self.liquidity_pct,
                                   self.amount_window)
            vol = vol.reindex(sorted(allow)).dropna()
            if vol.empty:
                return TargetPortfolio(as_of=ts, weights={})

        picked = vol.sort_values().head(self.top_n).index
        w = 1.0 / len(picked)
        return TargetPortfolio(as_of=ts, weights={str(s): w for s in picked})
