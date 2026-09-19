"""动态股票池：按 as_of_date 时间点求值的过滤器集合。

设计动机：如果把“今天的全部股票代码”直接拿去回测过去十年，
会产生严重的幸存者偏差（退市股被系统性忽略）。
本模块保证股票池回答的问题是：

    在某一个历史交易日，当时哪些股票是可投资股票？

所有判断只使用 as_of 当日及以前的信息。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import pandas as pd

from ..data.asof import latest_known
from ..data.loader import DataProvider


@dataclass(frozen=True)
class UniverseRules:
    """股票池过滤规则（全部按 as_of 时间点求值）。"""

    min_list_days: int = 60            # 上市满 N 个交易日（剔除次新股）
    exclude_st: bool = True            # 剔除当日 ST/*ST
    exclude_suspended: bool = True     # 剔除当日停牌
    exclude_delisted: bool = True      # 剔除已退市（delist_date <= as_of）
    index_membership: Optional[str] = None   # 预留：限定指数成分（需成分历史）
    min_market_cap: Optional[float] = None   # 预留：最小市值（需市值历史）
    max_market_cap: Optional[float] = None   # 预留：最大市值
    industries: Optional[Sequence[str]] = None    # 预留：行业白名单
    min_avg_daily_amount: Optional[float] = None  # 预留：流动性下限
    liquidity_lookback_days: int = 20

    @classmethod
    def from_yaml(cls, path) -> "UniverseRules":
        """从 universe.yaml 加载（嵌套键展平；未知键忽略）。"""
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        kwargs: dict = {}
        simple = {"min_list_days", "exclude_st", "exclude_suspended",
                  "exclude_delisted", "index_membership"}
        for key in simple:
            if key in raw:
                kwargs[key] = raw[key]
        mc = raw.get("market_cap") or {}
        if mc.get("min") is not None:
            kwargs["min_market_cap"] = mc["min"]
        if mc.get("max") is not None:
            kwargs["max_market_cap"] = mc["max"]
        liq = raw.get("liquidity") or {}
        if liq.get("min_avg_daily_amount") is not None:
            kwargs["min_avg_daily_amount"] = liq["min_avg_daily_amount"]
        if liq.get("lookback_days") is not None:
            kwargs["liquidity_lookback_days"] = liq["lookback_days"]
        ind = raw.get("industry") or {}
        if ind.get("include"):
            kwargs["industries"] = ind["include"]
        return cls(**kwargs)


class UniverseBuilder:
    """构建某历史交易日“当时可投资”的股票列表。

    判断依据（全部 <= as_of）：
      * 上市天数：list_date 早于 as_of 前 min_list_days 个交易日；
      * 退市：delist_date 缺失或 > as_of；
      * ST/停牌：as_of 当日（或此前最后已知）状态；
      * 流动性：截至 as_of 的滚动日均成交额。
    """

    def __init__(self, provider: DataProvider, rules: UniverseRules) -> None:
        self._provider = provider
        self._rules = rules

    @property
    def rules(self) -> UniverseRules:
        return self._rules

    def get_universe(self, as_of, *, prev_trading_days=None) -> list[str]:
        """返回 as_of 日可投资股票代码（按 symbol 排序）。

        prev_trading_days: as_of 及之前的交易日序列，缺省时从日历推导。
        """
        rules = self._rules
        ts = pd.Timestamp(as_of).normalize()

        # 预留过滤项：缺数据时明确报错，而不是静默忽略
        if rules.index_membership is not None:
            raise NotImplementedError(
                "index_membership 过滤需要指数成分变更历史数据，接入后再启用")
        if rules.min_market_cap is not None or rules.max_market_cap is not None:
            raise NotImplementedError(
                "市值过滤需要市值历史数据（bars.market_cap 列），接入后再启用")
        if rules.industries:
            raise NotImplementedError(
                "行业过滤需要行业分类历史数据，接入后再启用")

        cal = self._provider.get_calendar()
        if prev_trading_days is None:
            prev_trading_days = cal.slice(cal.dates[0], ts)
        hist = pd.DatetimeIndex(prev_trading_days)
        if len(hist) == 0:
            return []

        inst = self._provider.get_instruments()
        if rules.min_list_days > 0:
            if len(hist) <= rules.min_list_days:
                return []
            cutoff = hist[len(hist) - rules.min_list_days - 1]
            mask = inst["list_date"] <= cutoff
        else:
            mask = inst["list_date"] <= hist[-1]
        if rules.exclude_delisted:
            mask &= inst["delist_date"].isna() | (inst["delist_date"] > ts)
        candidates = sorted(inst.loc[mask, "symbol"].astype(str).tolist())

        if rules.exclude_st or rules.exclude_suspended:
            status = self._provider.get_status(hist[0], ts)
            last = latest_known(status, ts)
            if not last.empty:
                st_map = dict(zip(last["symbol"].astype(str), last["is_st"].astype(bool)))
                susp_map = dict(zip(last["symbol"].astype(str), last["is_suspended"].astype(bool)))
                candidates = [
                    s for s in candidates
                    if not (rules.exclude_st and st_map.get(s, False))
                    and not (rules.exclude_suspended and susp_map.get(s, False))
                ]

        if rules.min_avg_daily_amount is not None:
            lookback = max(1, rules.liquidity_lookback_days)
            start = hist[max(0, len(hist) - lookback - 1)]
            bars = self._provider.get_bars(start, ts)
            if not bars.empty:
                avg = bars.groupby("symbol")["amount"].mean()
                candidates = [
                    s for s in candidates
                    if s in avg.index and float(avg.loc[s]) >= rules.min_avg_daily_amount
                ]
        return candidates
