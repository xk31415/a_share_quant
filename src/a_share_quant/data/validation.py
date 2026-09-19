"""数据质量校验。所有进入回测的数据必须先通过 validate_*。"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .schema import (
    BAR_REQUIRED_COLUMNS,
    INSTRUMENT_REQUIRED_COLUMNS,
    STATUS_REQUIRED_COLUMNS,
)
from ..utils.dates import TradingCalendar


class DataValidationError(ValueError):
    """数据校验失败（致命问题）。"""


def validate_bars(df: pd.DataFrame) -> list[str]:
    """校验日线行情。

    致命问题（抛 DataValidationError）：缺列、(trade_date, symbol) 重复、
    high/low 与 open/close 矛盾、价格为负。
    非致命问题（返回 warning 列表）：必要列含 NaN、adj_close 非正。
    """
    issues: list[str] = []
    missing = [c for c in BAR_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataValidationError(f"bars missing required columns: {missing}")
    if df.empty:
        issues.append("bars is empty")
        return issues

    n_dup = int(df.duplicated(subset=["trade_date", "symbol"]).sum())
    if n_dup:
        raise DataValidationError(f"bars have {n_dup} duplicated (trade_date, symbol) rows")

    num = df[["open", "high", "low", "close"]]
    bad_ohlc = (
        (df["high"] < num[["open", "close", "low"]].max(axis=1))
        | (df["low"] > num[["open", "close", "high"]].min(axis=1))
        | (df["high"] < df["low"])
    )
    if int(bad_ohlc.sum()):
        raise DataValidationError(f"bars have {int(bad_ohlc.sum())} rows violating high/low consistency")

    for col in ("open", "high", "low", "close", "volume", "amount"):
        n_neg = int((df[col] < 0).sum())
        if n_neg:
            raise DataValidationError(f"column {col!r} has {n_neg} negative values")

    for col, n in df[list(BAR_REQUIRED_COLUMNS)].isna().sum().items():
        if n:
            issues.append(f"column {col!r} has {int(n)} NaN values")
    if "adj_close" in df.columns:
        n_nonpos = int((df["adj_close"] <= 0).sum())
        if n_nonpos:
            issues.append(f"adj_close has {n_nonpos} non-positive values")
    return issues


def validate_instruments(df: pd.DataFrame) -> list[str]:
    """校验证券基本信息表。"""
    missing = [c for c in INSTRUMENT_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataValidationError(f"instruments missing required columns: {missing}")
    if df.empty:
        raise DataValidationError("instruments is empty")
    n_dup = int(df["symbol"].duplicated().sum())
    if n_dup:
        raise DataValidationError(f"instruments have {n_dup} duplicated symbols")
    bad = df["delist_date"].notna() & (df["delist_date"] < df["list_date"])
    if int(bad.sum()):
        raise DataValidationError(f"instruments have {int(bad.sum())} rows with delist_date < list_date")
    issues: list[str] = []
    n_nat = int(df["list_date"].isna().sum())
    if n_nat:
        issues.append(f"instruments have {n_nat} rows with missing list_date")
    return issues


def validate_status(df: pd.DataFrame) -> list[str]:
    """校验每日状态表。"""
    missing = [c for c in STATUS_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataValidationError(f"status missing required columns: {missing}")
    if df.empty:
        raise DataValidationError("status is empty")
    n_dup = int(df.duplicated(subset=["trade_date", "symbol"]).sum())
    if n_dup:
        raise DataValidationError(f"status have {n_dup} duplicated (trade_date, symbol) rows")
    return []


def validate_dataset(bars: Optional[pd.DataFrame] = None,
                     instruments: Optional[pd.DataFrame] = None,
                     status: Optional[pd.DataFrame] = None,
                     calendar: Optional[TradingCalendar] = None) -> list[str]:
    """聚合校验：致命问题抛 DataValidationError，非致命问题以 warning 返回。"""
    warnings: list[str] = []
    if bars is not None:
        warnings.extend(validate_bars(bars))
    if instruments is not None:
        warnings.extend(validate_instruments(instruments))
    if status is not None:
        warnings.extend(validate_status(status))
    if bars is not None and instruments is not None:
        unknown = set(bars["symbol"]) - set(instruments["symbol"])
        if unknown:
            raise DataValidationError(
                f"bars contain {len(unknown)} symbols missing from instruments "
                f"(e.g. {sorted(unknown)[:5]})")
    if bars is not None and calendar is not None:
        bad_dates = set(pd.to_datetime(bars["trade_date"]).unique()) - set(calendar.dates)
        if bad_dates:
            raise DataValidationError(
                f"bars contain {len(bad_dates)} dates not in calendar "
                f"(e.g. {sorted(bad_dates)[:3]})")
    return warnings
