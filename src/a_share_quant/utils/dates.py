"""交易日历与日期运算。

项目中所有“下一交易日/上一交易日”语义必须经由 TradingCalendar，
不允许用自然日加减实现，以保证时间对齐可测试。
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable, Literal, Sequence

import pandas as pd

RebalanceFrequency = Literal["D", "W", "M"]


def _to_ts(value) -> pd.Timestamp:
    """归一化为当日零点的 Timestamp。"""
    return pd.Timestamp(value).normalize()


class TradingCalendar:
    """有序、去重的交易日集合。"""

    def __init__(self, dates: Iterable[_dt.date | str | pd.Timestamp]) -> None:
        idx = pd.DatetimeIndex(pd.to_datetime(list(dates))).normalize().sort_values()
        if len(idx) == 0:
            raise ValueError("TradingCalendar requires at least one trading date.")
        if idx.has_duplicates:
            raise ValueError("TradingCalendar contains duplicate dates.")
        self._dates = idx

    @classmethod
    def weekdays(cls, start, end) -> "TradingCalendar":
        """近似日历（仅剔除周末）。

        仅用于测试与合成数据，不可替代真实 A 股交易日历。
        """
        return cls(pd.bdate_range(start=_to_ts(start), end=_to_ts(end)))

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self._dates

    def __len__(self) -> int:
        return len(self._dates)

    def __contains__(self, date) -> bool:
        return _to_ts(date) in self._dates

    def is_trading_day(self, date) -> bool:
        return _to_ts(date) in self._dates

    def slice(self, start, end) -> pd.DatetimeIndex:
        """闭区间截取。"""
        return self._dates[(self._dates >= _to_ts(start)) & (self._dates <= _to_ts(end))]

    def next_trading_day(self, date) -> pd.Timestamp:
        """严格晚于 date 的第一个交易日。"""
        pos = self._dates.searchsorted(_to_ts(date), side="right")
        if pos >= len(self._dates):
            raise ValueError(f"no trading day after {date} in calendar")
        return self._dates[pos]

    def prev_trading_day(self, date) -> pd.Timestamp:
        """严格早于 date 的最后一个交易日。"""
        pos = self._dates.searchsorted(_to_ts(date), side="left")
        if pos == 0:
            raise ValueError(f"no trading day before {date} in calendar")
        return self._dates[pos - 1]

    def shift(self, date, n: int) -> pd.Timestamp:
        """从 date（必须是交易日）向前/向后移动 n 个交易日。"""
        ts = _to_ts(date)
        if ts not in self._dates:
            raise ValueError(f"{date} is not a trading day")
        pos = int(self._dates.get_loc(ts))
        new_pos = pos + n
        if not 0 <= new_pos < len(self._dates):
            raise ValueError(f"shift({date}, {n}) out of calendar range")
        return self._dates[new_pos]


def rebalance_dates(trading_days: pd.DatetimeIndex,
                    freq: RebalanceFrequency) -> pd.DatetimeIndex:
    """选取每个周期内的最后一个交易日作为信号日。

    信号于该日收盘生成，次一交易日成交（由回测引擎保证）。
    freq: "D"=每日, "W"=每周, "M"=每月。
    """
    if freq == "D":
        return trading_days
    if freq not in ("W", "M"):
        raise ValueError(f"unsupported rebalance frequency: {freq!r}")
    periods = trading_days.to_period("W" if freq == "W" else "M")
    last_per_period = pd.Series(trading_days, index=periods).groupby(level=0).max()
    return pd.DatetimeIndex(sorted(last_per_period.tolist()))
