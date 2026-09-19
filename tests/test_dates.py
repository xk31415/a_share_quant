"""日期对齐与交易日历测试。"""
import pandas as pd
import pytest

from a_share_quant.utils.dates import TradingCalendar, rebalance_dates


def test_calendar_next_prev_trading_day():
    cal = TradingCalendar.weekdays("2021-01-04", "2021-01-15")
    # 2021-01-08 是周五，下一交易日是周一 01-11
    assert cal.next_trading_day("2021-01-08") == pd.Timestamp("2021-01-11")
    assert cal.prev_trading_day("2021-01-11") == pd.Timestamp("2021-01-08")
    assert cal.is_trading_day("2021-01-09") is False
    assert cal.is_trading_day("2021-01-08") is True


def test_calendar_shift_and_bounds():
    cal = TradingCalendar.weekdays("2021-01-04", "2021-01-29")
    assert cal.shift("2021-01-04", 3) == pd.Timestamp("2021-01-07")
    with pytest.raises(ValueError):
        cal.shift("2021-01-04", -1)   # 越界
    with pytest.raises(ValueError):
        cal.shift("2021-01-09", 1)    # 非交易日


def test_calendar_rejects_duplicates_and_empty():
    with pytest.raises(ValueError):
        TradingCalendar(["2021-01-04", "2021-01-04"])
    with pytest.raises(ValueError):
        TradingCalendar([])


def test_rebalance_dates_monthly_picks_last_trading_day():
    days = pd.bdate_range("2021-01-01", "2021-03-31")
    rb = rebalance_dates(days, "M")
    assert list(rb) == [
        pd.Timestamp("2021-01-29"),
        pd.Timestamp("2021-02-26"),
        pd.Timestamp("2021-03-31"),
    ]


def test_rebalance_dates_weekly():
    days = pd.bdate_range("2021-01-04", "2021-01-15")
    rb = rebalance_dates(days, "W")
    assert list(rb) == [pd.Timestamp("2021-01-08"), pd.Timestamp("2021-01-15")]


def test_rebalance_dates_daily_and_invalid():
    days = pd.bdate_range("2021-01-04", "2021-01-08")
    assert list(rebalance_dates(days, "D")) == list(days)
    with pytest.raises(ValueError):
        rebalance_dates(days, "Y")
