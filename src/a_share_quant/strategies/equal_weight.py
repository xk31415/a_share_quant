"""基准策略：股票池等权 + 定期调仓。

这是第一套“框架验证策略”：固定规则股票池 + 等权持仓 + 定期调仓。
目的只是测试数据读取、股票池、调仓、T+1、交易成本、净值与绩效计算，
不是研究的最终策略。
"""
from __future__ import annotations

import hashlib
from typing import Sequence

import pandas as pd

from .base import Strategy, TargetPortfolio
from .filters import liquid_symbols


def _stable_key(symbol: str, seed: int = 0) -> str:
    """与运行环境无关的稳定散列（Python 内置 hash 每个进程会变）。

    ``seed`` 用来换一批抽样：换个 seed 等价于"再随机抽一次"，
    可以检验基准收益里有多少来自运气。

    **seed=0 必须沿用最初的 ``sha1(symbol)``**，否则给 key 加个前缀就会
    静默换掉整个种子 0 的持仓，历史实验（如 EXP-20261020-202913）再也复现不了。
    这个坑踩过一次：加 seed 支持后种子 0 的年化从 9.15% 变成 4.55%，
    而代码"看起来"只是多了一个默认参数。
    """
    payload = f"{seed}:{symbol}" if seed else str(symbol)
    return hashlib.sha1(payload.encode()).hexdigest()


class EqualWeightStrategy(Strategy):
    """对当日股票池等权配置；超过 max_positions 时用**稳定散列**抽样。

    为什么不用字典序：在 4700 只的真实宇宙里 ``sorted(universe)[:20]`` 永远取
    代码最小的 20 只（000001~000030），基准会退化成"最小代码 20 只等权"，
    收益与市场无关。改用 ``sha1(symbol)`` 排序后截断，等价于**无放回随机抽样**，
    但对同一份股票池结果固定、跨机器可复现。

    说明：更合理的基准是按市值/流动性加权；此处只保证"无偏且可复现"，
    因为该策略只为框架冒烟测试服务。
    """

    name = "equal_weight"

    def __init__(self, max_positions: int = 20, seed: int = 0,
                 liquidity_pct: float | None = None) -> None:
        if max_positions < 1:
            raise ValueError("max_positions must be >= 1")
        self.max_positions = max_positions
        self.seed = int(seed)
        # 只保留成交额最高的前 liquidity_pct；None = 不限制。
        # 用来给"因子组合"提供同流动性档的等权对照（默认不变，输出逐位一致）。
        self.liquidity_pct = liquidity_pct

    def generate_target(self, as_of, bars: pd.DataFrame,
                        universe: Sequence[str]) -> TargetPortfolio:
        pool = universe
        if self.liquidity_pct is not None:
            allow = liquid_symbols(bars, as_of, universe, self.liquidity_pct)
            pool = [s for s in universe if str(s) in allow]
        picked = sorted(pool, key=lambda s: _stable_key(s, self.seed))[: self.max_positions]
        ts = pd.Timestamp(as_of).normalize()
        if not picked:
            return TargetPortfolio(as_of=ts, weights={})
        w = 1.0 / len(picked)
        return TargetPortfolio(as_of=ts, weights={s: w for s in picked})
