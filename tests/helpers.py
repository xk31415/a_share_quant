"""共享测试工具：内存版 DataProvider 与小型行情构造器。"""
from __future__ import annotations

import pandas as pd

from a_share_quant.data.loader import DataProvider
from a_share_quant.utils.dates import TradingCalendar


class FakeProvider(DataProvider):
    """最小内存实现，用于测试与快速实验。"""

    def __init__(self, bars: pd.DataFrame, instruments: pd.DataFrame,
                 status: pd.DataFrame, calendar: TradingCalendar) -> None:
        self.bars = bars.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        self.instruments_df = instruments
        self.status_df = status.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        self._calendar = calendar

    def get_bars(self, start, end, symbols=None) -> pd.DataFrame:
        mask = ((self.bars["trade_date"] >= pd.Timestamp(start))
                & (self.bars["trade_date"] <= pd.Timestamp(end)))
        df = self.bars.loc[mask]
        if symbols is not None:
            df = df.loc[df["symbol"].isin(set(symbols))]
        return df.reset_index(drop=True)

    def get_instruments(self) -> pd.DataFrame:
        return self.instruments_df.copy()

    def get_status(self, start, end, symbols=None) -> pd.DataFrame:
        mask = ((self.status_df["trade_date"] >= pd.Timestamp(start))
                & (self.status_df["trade_date"] <= pd.Timestamp(end)))
        df = self.status_df.loc[mask]
        if symbols is not None:
            df = df.loc[df["symbol"].isin(set(symbols))]
        return df.reset_index(drop=True)

    def get_calendar(self) -> TradingCalendar:
        return self._calendar


def make_bars(dates, symbols, price: float = 10.0) -> pd.DataFrame:
    """构造规则上涨行情（每日 +0.1%），OHLC 自洽、开盘不触及涨跌停。"""
    rows = []
    for j, s in enumerate(symbols):
        px = price * (1 + 0.001 * j)
        for i, d in enumerate(dates):
            close = px * (1.001 ** i)
            open_px = px * (1.001 ** (i - 1)) if i > 0 else px
            rows.append({
                "trade_date": pd.Timestamp(d), "symbol": s,
                "open": round(open_px, 4),
                "high": round(max(open_px, close) * 1.005, 4),
                "low": round(min(open_px, close) * 0.995, 4),
                "close": round(close, 4),
                "volume": 1_000_000.0,
                "amount": round(close * 1_000_000, 2),
                "adj_close": round(close, 4),
                "pre_close": round(open_px, 4),
                "limit_up": round(open_px * 1.10, 4),
                "limit_down": round(open_px * 0.90, 4),
                "is_suspended": False,
                "is_st": False,
            })
    return pd.DataFrame(rows)


def make_provider(dates, symbols, instruments=None, status=None,
                  bars=None, price: float = 10.0) -> FakeProvider:
    """构造一套最小可用的数据环境。"""
    dates = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    if bars is None:
        bars = make_bars(dates, symbols, price=price)
    if instruments is None:
        instruments = pd.DataFrame([
            {"symbol": s, "name": f"股票{s}",
             "list_date": dates[0] - pd.Timedelta(days=365),
             "delist_date": pd.NaT}
            for s in symbols])
    if status is None:
        status = pd.DataFrame([
            {"trade_date": d, "symbol": s, "is_st": False, "is_suspended": False}
            for d in dates for s in symbols])
    return FakeProvider(bars, instruments, status, TradingCalendar(dates))
