"""增量更新纯逻辑测试。

这几条规则出错的表现都很隐蔽：不报错、数据看着还在，
但某几天或某几只股票的行被静默删掉了。所以每条都要锁。
"""
from __future__ import annotations

import pandas as pd
import pytest

from a_share_quant.data.incremental import (
    atomic_write,
    check_continuity,
    upsert,
)

D = pd.to_datetime


def _frame(rows):
    return pd.DataFrame(rows, columns=["trade_date", "symbol", "close"]).astype(
        {"trade_date": "datetime64[ns]", "close": float})


def test_upsert_replaces_overlapping_rows():
    old = _frame([(D("2026-10-19"), "A", 10.0), (D("2026-10-20"), "A", 10.5)])
    new = _frame([(D("2026-10-20"), "A", 11.0)])      # 源修订了收盘价
    out = upsert(old, new)
    assert len(out) == 2
    assert out.loc[out["trade_date"] == D("2026-10-20"), "close"].iloc[0] == 11.0


def test_upsert_keeps_old_rows_absent_from_new_window():
    """窗口里没有行情的停牌股，旧记录必须保留。

    这是我第一版写错的地方：用 ``old[trade_date < new.min()]`` 会把
    窗口内所有行一起丢掉，等于用一次更新制造数据缺口。
    """
    old = _frame([(D("2026-10-19"), "A", 10.0),
                  (D("2026-10-20"), "SUSP", 5.0)])   # SUSP 窗口内停牌，源不返回
    new = _frame([(D("2026-10-20"), "A", 10.5),
                  (D("2026-10-21"), "A", 10.8)])
    out = upsert(old, new)
    assert len(out) == 4, "2 行旧 + 2 行新，无重叠键"
    assert "SUSP" in set(out["symbol"]), "停牌股的历史记录被删掉了"
    assert out.loc[out["symbol"] == "SUSP", "trade_date"].iloc[0] == D("2026-10-20")


def test_upsert_appends_new_dates_without_duplicates():
    old = _frame([(D("2026-10-19"), "A", 10.0)])
    new = _frame([(D("2026-10-20"), "A", 10.5), (D("2026-10-20"), "B", 3.0)])
    out = upsert(old, new)
    assert len(out) == 3
    assert not out.duplicated(subset=["trade_date", "symbol"]).any()
    assert out["trade_date"].max() == D("2026-10-20")


def test_upsert_is_sorted_and_stable_on_repeat():
    old = _frame([(D("2026-10-19"), "B", 1.0), (D("2026-10-19"), "A", 2.0)])
    once = upsert(old, _frame([(D("2026-10-20"), "A", 3.0)]))
    twice = upsert(once, _frame([(D("2026-10-20"), "A", 3.0)]))
    assert once.equals(twice), "同一天重复更新必须幂等"
    assert list(once["symbol"].head(2)) == ["A", "B"]


def test_upsert_empty_new_returns_copy():
    old = _frame([(D("2026-10-19"), "A", 10.0)])
    out = upsert(old, _frame([]))
    assert out.equals(old)
    assert out is not old, "应返回副本，避免调用方改到原表"


def test_upsert_rejects_missing_key():
    with pytest.raises(ValueError, match="缺少合并键"):
        upsert(pd.DataFrame({"trade_date": [], "close": []}),
               _frame([(D("2026-10-19"), "A", 1.0)]))


def test_check_continuity_flags_halved_day():
    bars = pd.DataFrame({
        "trade_date": [D("2026-10-19")] * 100 + [D("2026-10-20")] * 40,
        "symbol": [f"S{i}" for i in range(100)] + [f"S{i}" for i in range(40)]})
    problems = check_continuity(bars)
    assert len(problems) == 1 and "2026-10-20" in problems[0]


def test_check_continuity_passes_on_normal_change():
    bars = pd.DataFrame({
        "trade_date": [D("2026-10-19")] * 100 + [D("2026-10-20")] * 97,
        "symbol": [f"S{i}" for i in range(100)] + [f"S{i}" for i in range(97)]})
    assert check_continuity(bars) == []


def test_check_continuity_handles_single_day():
    bars = pd.DataFrame({"trade_date": [D("2026-10-19")], "symbol": ["A"]})
    assert check_continuity(bars) == []


def test_atomic_write_leaves_no_tmp_and_roundtrips(tmp_path):
    df = _frame([(D("2026-10-19"), "A", 10.0)])
    p = atomic_write(df, tmp_path / "bars.csv")
    assert p.exists()
    assert not list(tmp_path.glob("*.tmp"))
    back = pd.read_csv(p)
    assert len(back) == 1 and back["symbol"].iloc[0] == "A"


def test_atomic_write_overwrites_existing(tmp_path):
    p = tmp_path / "x.csv"
    atomic_write(_frame([(D("2026-10-19"), "A", 1.0)]), p)
    atomic_write(_frame([(D("2026-10-19"), "A", 2.0),
                         (D("2026-10-20"), "A", 3.0)]), p)
    assert len(pd.read_csv(p)) == 2
    assert not list(tmp_path.glob("*.tmp"))
