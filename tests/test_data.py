"""数据 schema 与校验测试。"""
import numpy as np
import pandas as pd
import pytest

from a_share_quant.data.schema import coerce_bars
from a_share_quant.data.validation import (
    DataValidationError,
    validate_bars,
    validate_dataset,
    validate_instruments,
    validate_status,
)
from a_share_quant.utils.dates import TradingCalendar

from helpers import make_bars, make_provider


def _bars():
    return make_bars(pd.bdate_range("2021-01-04", periods=10), ["AAA", "BBB"])


def test_validate_bars_missing_column():
    df = _bars().drop(columns=["close"])
    with pytest.raises(DataValidationError, match="missing required columns"):
        validate_bars(df)


def test_validate_bars_duplicate_rows():
    df = pd.concat([_bars(), _bars().iloc[[0]]], ignore_index=True)
    with pytest.raises(DataValidationError, match="duplicated"):
        validate_bars(df)


def test_validate_bars_ohlc_consistency():
    df = _bars()
    df.loc[0, "high"] = df.loc[0, "low"] - 1.0
    with pytest.raises(DataValidationError, match="high/low"):
        validate_bars(df)


def test_validate_bars_negative_price():
    df = _bars()
    df.loc[3, "volume"] = -1.0
    with pytest.raises(DataValidationError, match="negative"):
        validate_bars(df)


def test_validate_bars_nan_is_warning_not_fatal():
    df = _bars()
    df.loc[5, "close"] = np.nan
    issues = validate_bars(df)
    assert any("NaN" in i for i in issues)


def test_validate_instruments_rules():
    inst = pd.DataFrame([
        {"symbol": "AAA", "name": "a", "list_date": "2020-01-01", "delist_date": ""},
        {"symbol": "AAA", "name": "dup", "list_date": "2020-01-01", "delist_date": ""},
    ])
    with pytest.raises(DataValidationError, match="duplicated symbols"):
        validate_instruments(inst)
    inst2 = pd.DataFrame([
        {"symbol": "AAA", "name": "a", "list_date": "2020-06-01", "delist_date": "2020-01-01"},
    ])
    with pytest.raises(DataValidationError, match="delist_date < list_date"):
        validate_instruments(inst2)


def test_validate_status_duplicate():
    df = pd.DataFrame([
        {"trade_date": "2021-01-04", "symbol": "AAA", "is_st": False, "is_suspended": False},
        {"trade_date": "2021-01-04", "symbol": "AAA", "is_st": False, "is_suspended": False},
    ])
    with pytest.raises(DataValidationError, match="duplicated"):
        validate_status(df)


def test_validate_dataset_cross_checks():
    dates = pd.bdate_range("2021-01-04", periods=5)
    provider = make_provider(dates, ["AAA", "BBB"])
    bars = provider.get_bars(dates[0], dates[-1])
    inst = provider.get_instruments().loc[lambda x: x["symbol"] == "AAA"]  # 少一个
    with pytest.raises(DataValidationError, match="missing from instruments"):
        validate_dataset(bars=bars, instruments=inst)

    # 行情日期不在日历中（日历只到 01-07，行情有 01-08）
    cal = TradingCalendar.weekdays("2021-01-04", "2021-01-07")
    with pytest.raises(DataValidationError, match="not in calendar"):
        validate_dataset(bars=bars, calendar=cal)


def test_coerce_bars_sorts_and_casts():
    df = _bars().sample(frac=1.0, random_state=0)  # 打乱顺序
    out = coerce_bars(df)
    assert out.equals(coerce_bars(out))  # 幂等
    assert out[["open", "close"]].dtypes.eq("float64").all()
    assert out["is_st"].dtype == bool
