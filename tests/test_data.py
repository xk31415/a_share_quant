"""数据 schema 与校验测试。"""
import numpy as np
import pandas as pd
import pytest

from a_share_quant.data.schema import coerce_bars
from a_share_quant.data.validation import (
    DataValidationError,
    check_survivorship_bias,
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


def test_coerce_bars_string_booleans_are_parsed_by_literal_value():
    """CSV 里的字符串 "False" 不得被 bool() 解析成 True。

    历史缺陷：``.astype(bool)`` 对 object 列调用的是 ``bool("False") == True``，
    会把"未停牌"整列静默变成"停牌"。
    """
    df = make_bars(pd.bdate_range("2021-01-04", periods=3), ["AAA"])
    df["is_suspended"] = ["False", "True", "False"]
    out = coerce_bars(df)
    assert out["is_suspended"].tolist() == [False, True, False]


def test_coerce_bars_missing_bool_values_default_false():
    df = make_bars(pd.bdate_range("2021-01-04", periods=3), ["AAA"])
    df["is_suspended"] = pd.Series([True, None, False], dtype=object)
    out = coerce_bars(df)
    assert out["is_suspended"].tolist() == [True, False, False]


# ---------- 幸存者偏差哨兵 ----------

def _long_sample(days=300):
    """构造：AAA 全程在市；BBB 在第 100 个交易日退市（已正确标记）。"""
    dates = pd.bdate_range("2021-01-04", periods=days)
    rows = []
    for d in dates[:100]:
        rows.append({"trade_date": d, "symbol": "BBB", "open": 10.0, "high": 10.5,
                     "low": 9.5, "close": 10.0, "volume": 1e6, "amount": 1e7})
    for d in dates:
        rows.append({"trade_date": d, "symbol": "AAA", "open": 10.0, "high": 10.5,
                     "low": 9.5, "close": 10.0, "volume": 1e6, "amount": 1e7})
    bars = pd.DataFrame(rows)
    instruments = pd.DataFrame([
        {"symbol": "AAA", "name": "a", "list_date": dates[0], "delist_date": pd.NaT},
        {"symbol": "BBB", "name": "b", "list_date": dates[0], "delist_date": dates[99]},
    ])
    return bars, instruments


def test_survivorship_sentinel_silent_when_delist_marked_correctly():
    """退市股已正确标记 delist_date -> 不应产生告警。"""
    bars, inst = _long_sample()
    assert check_survivorship_bias(inst, bars) == []


def test_survivorship_sentinel_detects_missing_delist_mark():
    """BBB 行情中途消失但未标记退市 -> 必须告警（股票池会误把它当可投资）。"""
    bars, inst = _long_sample()
    inst.loc[inst["symbol"] == "BBB", "delist_date"] = pd.NaT  # 抹掉退市标记
    warns = check_survivorship_bias(inst, bars)
    assert len(warns) == 2  # 无退市股 + 未标记的中途消失
    assert any(" BBB " in w or "'BBB'" in w or "['BBB']" in w for w in warns)
    assert any("delist_date 为空" in w for w in warns)


def test_survivorship_sentinel_warns_when_no_delisting_at_all():
    """整个 instruments 无退市记录 -> 数据源可能只有"当前上市股票"。"""
    bars, inst = _long_sample()
    inst2 = inst.loc[inst["symbol"] == "AAA"].copy()
    bars2 = bars.loc[bars["symbol"] == "AAA"].copy()
    warns = check_survivorship_bias(inst2, bars2)
    assert len(warns) == 1
    assert "survivorship" in warns[0] and "没有任何已退市证券" in warns[0]


def test_survivorship_sentinel_ignores_symbols_ending_at_sample_end():
    """数据末端附近结束的标的属正常上市状态，不应被判为"消失"。"""
    bars, inst = _long_sample()
    # BBB 行情延到最后一天附近（只差 30 个自然日）
    extra = pd.bdate_range("2021-01-04", periods=300)[-10:]
    rows = [{"trade_date": d, "symbol": "BBB", "open": 10.0, "high": 10.5, "low": 9.5,
             "close": 10.0, "volume": 1e6, "amount": 1e7} for d in extra]
    bars2 = pd.concat([bars, pd.DataFrame(rows)], ignore_index=True)
    inst2 = inst.copy()
    inst2.loc[inst2["symbol"] == "BBB", "delist_date"] = pd.NaT
    # max_missing_days=90 -> 尾部 10 天在容忍范围内，不告警（仅剩"无退市股"告警）
    warns = check_survivorship_bias(inst2, bars2, max_missing_days=90)
    assert all("delist_date 为空" not in w for w in warns)


def test_validate_dataset_includes_survivorship_warnings():
    """哨兵检查必须接入聚合校验，不能只在外部单独调用才生效。"""
    bars, inst = _long_sample()
    inst.loc[inst["symbol"] == "BBB", "delist_date"] = pd.NaT
    warns = validate_dataset(bars=bars, instruments=inst)
    assert any("survivorship" in w for w in warns)
