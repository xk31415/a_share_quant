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


def check_survivorship_bias(instruments: Optional[pd.DataFrame] = None,
                            bars: Optional[pd.DataFrame] = None,
                            max_missing_days: int = 90) -> list[str]:
    """幸存者偏差哨兵：识别"退市股整体缺失"与"中途消失却未标记退市"。

    为什么需要它（这是数据层防线，不是股票池防线）：
        ``universe/builder.py`` 能在历史时点正确剔除未来退市股，**前提**是数据源
        确实包含了这些股票及其 ``delist_date``。若数据源只提供"当前仍在上市"的
        证券，回测结果会被系统性高估，而股票池逻辑本身完全无法察觉——
        本检查把这个隐患显式暴露出来。

    两类告警：
      1. instruments 中 ``delist_date`` 全为空 —— 可能整个样本都没有退市股；
      2. 某些证券的行情在数据末端之前就消失，但没有被标记为已退市 ——
         股票池会把它们继续当成可投资证券（``builder`` 按 delist_date 过滤）。

    一律返回 warning（不抛异常）：缺少退市股在某些合法数据集中确实存在，
    是否需要人工确认应由使用者决定，但绝不能静默通过。
    """
    warnings: list[str] = []
    if instruments is None or instruments.empty:
        return warnings

    has_delist = "delist_date" in instruments.columns
    if has_delist and int(instruments["delist_date"].notna().sum()) == 0:
        warnings.append(
            "survivorship: instruments 中没有任何已退市证券（delist_date 全为空）。"
            "若该数据源只包含当前仍在上市的股票，历史回测将存在系统性幸存者偏差——"
            "请确认数据源确实包含退市股票及其退市日期。")

    if bars is None or bars.empty or not has_delist:
        return warnings

    end = pd.to_datetime(bars["trade_date"]).max()
    last_seen = bars.groupby("symbol")["trade_date"].max()
    delist_flag = instruments.set_index("symbol")["delist_date"]
    vanished: list[str] = []
    for sym, seen in last_seen.items():
        seen = pd.Timestamp(seen)
        if (end - seen).days <= max_missing_days:
            continue  # 数据末端附近才结束 -> 仍在上市，属正常情况
        if pd.isna(delist_flag.get(sym)):
            vanished.append(str(sym))
    if vanished:
        warnings.append(
            f"survivorship: {len(vanished)} 个证券在数据中提前消失（末次行情早于数据末端"
            f" {max_missing_days} 个自然日以上），但 delist_date 为空"
            f"（例如 {vanished[:5]}）。这些标的可能已退市却未被标记，"
            f"股票池会把它们继续当成可投资证券。")
    return warnings


def validate_dataset(bars: Optional[pd.DataFrame] = None,
                     instruments: Optional[pd.DataFrame] = None,
                     status: Optional[pd.DataFrame] = None,
                     calendar: Optional[TradingCalendar] = None) -> list[str]:
    """聚合校验：致命问题抛 DataValidationError，非致命问题以 warning 返回。

     warning 中总是包含幸存者偏差哨兵检查的结果（隐患必须显式暴露，不得静默通过）。
    """
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
    warnings.extend(check_survivorship_bias(instruments, bars))
    return warnings
