"""数据集装配与自检的离线测试（不需要掘金 / QMT SDK）。"""
import pandas as pd
import pytest

from a_share_quant.data.builders import (
    DEFAULT_BOARDS,
    assemble,
    classify_board,
    is_index,
    select_boards,
    self_check,
    split_symbol,
)
from a_share_quant.data.validation import DataValidationError


def _symbols():
    return [
        ("600000", "SHSE"), ("000001", "SZSE"), ("300001", "SZSE"),
        ("688001", "SHSE"), ("000300", "SHSE"),      # 科创板 + 指数
    ]


def _frames(days=5, missing_limits=False, bad_k=False, delisted=True,
            include_suspend_row=False):
    dates = pd.bdate_range("2024-01-02", periods=days)
    inst_rows, raw_rows, post_rows, status_rows = [], [], [], []
    for sym, ex in _symbols():
        inst_rows.append({
            "symbol": sym, "exchange": ex, "name": sym,
            "list_date": pd.Timestamp("2010-01-01"),
            "delist_date": pd.Timestamp("2024-06-01") if (delisted and sym == "600000") else pd.NaT,
        })
        for i, d in enumerate(dates):
            close = 10.0 + i * 0.1
            raw_rows.append({"trade_date": d, "symbol": sym, "open": close,
                             "high": close * 1.01, "low": close * 0.99,
                             "close": close, "volume": 1e6, "amount": close * 1e6,
                             "pre_close": close - 0.1})
            cum = 1.0 if not bad_k else float(1.0 - 0.05 * i)   # 递减的 k -> 应报错
            post_rows.append({"trade_date": d, "symbol": sym, "adj_close": close * cum})
            status_rows.append({
                "trade_date": d, "symbol": sym, "is_st": False,
                "is_suspended": False,
                "limit_up": float("nan") if missing_limits else round(close * 1.1, 2),
                "limit_down": float("nan") if missing_limits else round(close * 0.9, 2),
            })
    if include_suspend_row:
        status_rows.append({"trade_date": dates[-1], "symbol": "000001", "is_st": False,
                            "is_suspended": True, "limit_up": 11.0, "limit_down": 9.0})
    return (pd.DataFrame(raw_rows), pd.DataFrame(post_rows), pd.DataFrame(status_rows),
            pd.DataFrame(inst_rows), dates)


def test_split_symbol_handles_all_formats():
    assert split_symbol("SHSE.600000") == ("600000", "SHSE")
    assert split_symbol("600000.SH") == ("600000", "SHSE")
    assert split_symbol("SZSE.000001") == ("000001", "SZSE")
    assert split_symbol("300001") == ("300001", "")


def test_classify_board_and_index():
    assert classify_board("600000", "SHSE") == "main_sh"
    assert classify_board("000001", "SZSE") == "main_sz"
    assert classify_board("300001", "SZSE") == "chinext"
    assert classify_board("688001", "SHSE") == "star"
    assert classify_board("830799", "BJSE") == "bse"
    assert classify_board("999999") == "unknown"
    # 同一前缀在不同交易所含义不同：沪深300 指数 vs 平安银行
    assert is_index("000300", "SHSE") is True
    assert is_index("000001", "SZSE") is False


def test_select_boards_default_excludes_star_and_bse():
    raw, post, status, inst, dates = _frames()
    picked = select_boards(inst, list(DEFAULT_BOARDS))
    assert set(picked["symbol"]) == {"600000", "000001", "300001"}


def test_assemble_drops_index_and_filters_boards():
    raw, post, status, inst, dates = _frames()
    report = assemble(raw, post, status, inst, dates)
    assert set(report.bars["symbol"]) == {"600000", "000001", "300001"}
    assert "000300" not in set(report.instruments["symbol"])
    assert any("指数" in w for w in report.warnings)


def test_assemble_merges_adj_close_and_limits():
    raw, post, status, inst, dates = _frames()
    report = assemble(raw, post, status, inst, dates)
    row = report.bars.iloc[0]
    assert row["adj_close"] == pytest.approx(row["close"])
    assert row["limit_up"] == pytest.approx(round(row["close"] * 1.1, 2))
    assert row["limit_exempt"] is False or row["limit_exempt"] == False  # noqa: E712


def test_zero_limit_is_exempt_but_missing_is_not():
    """涨跌停价显式为 0（当日无涨跌幅）才算豁免；单纯缺失不算。"""
    raw, post, status, inst, dates = _frames(missing_limits=True)
    status.loc[status["symbol"] == "600000", "limit_up"] = 0.0
    report = assemble(raw, post, status, inst, dates)
    exempt = report.bars[report.bars["symbol"] == "600000"]
    assert exempt["limit_exempt"].all()
    assert exempt["limit_up"].isna().all()          # 豁免行的价格被置空，执行层才跳过
    missing = report.bars[report.bars["symbol"] != "600000"]
    assert (~missing["limit_exempt"]).all()


def test_self_check_rejects_dataset_without_delisted():
    raw, post, status, inst, dates = _frames(delisted=False)
    report = assemble(raw, post, status, inst, dates)
    with pytest.raises(DataValidationError, match="退市"):
        self_check(report)


def test_self_check_rejects_missing_limits():
    raw, post, status, inst, dates = _frames(missing_limits=True)
    report = assemble(raw, post, status, inst, dates)
    with pytest.raises(DataValidationError, match="涨跌停"):
        self_check(report)
    # 显式放行时只告警不报错
    self_check(report, allow_missing_limits=True, min_symbols_per_day=1)


def test_self_check_rejects_non_monotonic_adj_factor():
    """k 递减说明拿到的是前复权或复权价没对齐。"""
    raw, post, status, inst, dates = _frames(bad_k=True)
    report = assemble(raw, post, status, inst, dates)
    with pytest.raises(DataValidationError, match="单调不减"):
        self_check(report, min_symbols_per_day=1)


def test_self_check_rejects_non_string_symbol():
    raw, post, status, inst, dates = _frames()
    report = assemble(raw, post, status, inst, dates)
    report.bars["symbol"] = report.bars["symbol"].astype(int)
    with pytest.raises(DataValidationError):
        self_check(report, min_symbols_per_day=1)


def test_self_check_rejects_dates_outside_calendar():
    raw, post, status, inst, dates = _frames()
    report = assemble(raw, post, status, inst, dates)
    report.calendar = report.calendar.iloc[1:]      # 砍掉第一天
    with pytest.raises(DataValidationError, match="交易日历"):
        self_check(report, min_symbols_per_day=1)


def test_self_check_warns_on_thin_trading_days():
    raw, post, status, inst, dates = _frames()
    report = assemble(raw, post, status, inst, dates)
    warnings = self_check(report, min_symbols_per_day=100)
    assert any("证券数少于" in w for w in warnings)


def test_self_check_passes_on_clean_dataset():
    raw, post, status, inst, dates = _frames()
    report = assemble(raw, post, status, inst, dates)
    self_check(report, min_symbols_per_day=1)       # 不抛异常即通过


# ---------- 涨跌停规则表 ----------

def test_limit_ratio_by_board_and_st():
    from a_share_quant.data.builders import limit_ratio
    assert limit_ratio("600000", "SHSE", "2023-06-01", False) == 0.10
    assert limit_ratio("600000", "SHSE", "2023-06-01", True) == 0.05    # ST ±5%
    assert limit_ratio("000001", "SZSE", "2023-06-01", False) == 0.10
    # 创业板注册制改革：2020-08-24 前后 10% -> 20%
    assert limit_ratio("300001", "SZSE", "2020-08-21", False) == 0.10
    assert limit_ratio("300001", "SZSE", "2020-08-24", False) == 0.20
    assert limit_ratio("300001", "SZSE", "2023-06-01", True) == 0.20    # 创业板 ST 仍 20%
    assert limit_ratio("688001", "SHSE", "2023-06-01", False) == 0.20


def _limit_frames(pre_close=10.0, close=10.5, is_st=False, days=4):
    dates = pd.bdate_range("2024-01-02", periods=days)
    bars = pd.DataFrame({
        "trade_date": dates, "symbol": "600000", "open": close, "high": close,
        "low": close, "close": close, "volume": 1e6, "amount": close * 1e6,
        "pre_close": pre_close, "adj_close": close, "limit_up": float("nan"),
        "limit_down": float("nan"), "limit_exempt": False,
        "is_st": is_st, "is_suspended": False,
    })
    inst = pd.DataFrame([{"symbol": "600000", "exchange": "SHSE", "name": "x",
                          "list_date": pd.Timestamp("2010-01-01"),
                          "delist_date": pd.NaT}])
    cal = pd.DataFrame({"trade_date": dates})
    return bars, inst, cal


def test_derive_limits_uses_rule_table():
    from a_share_quant.data.builders import derive_limits
    bars, inst, cal = _limit_frames(pre_close=10.0, close=10.5)
    out, _ = derive_limits(bars, inst, cal)
    assert out["limit_up"].iloc[0] == pytest.approx(11.0)
    assert out["limit_down"].iloc[0] == pytest.approx(9.0)
    assert not out["limit_exempt"].any()


def test_derive_limits_st_narrows_band():
    from a_share_quant.data.builders import derive_limits
    bars, inst, cal = _limit_frames(pre_close=10.0, close=10.4, is_st=True)
    out, _ = derive_limits(bars, inst, cal)
    assert out["limit_up"].iloc[0] == pytest.approx(10.5)   # ST +5%


def test_derive_limits_rejects_price_beyond_limit():
    """收盘价越过推算出的涨停 -> 规则表或 is_st 有误，必须报错而不是放宽约束。"""
    from a_share_quant.data.builders import derive_limits
    bars, inst, cal = _limit_frames(pre_close=10.0, close=11.5)
    with pytest.raises(DataValidationError, match="涨跌停"):
        derive_limits(bars, inst, cal)


def test_derive_limits_exempts_new_listing():
    """注册制新股上市前 5 个交易日不设涨跌幅 -> limit_exempt。"""
    from a_share_quant.data.builders import derive_limits
    bars, inst, cal = _limit_frames(pre_close=10.0, close=10.2, days=4)
    inst.loc[0, "list_date"] = pd.Timestamp("2024-01-02")
    inst.loc[0, "exchange"] = "SZSE"
    inst.loc[0, "symbol"] = "300001"
    bars["symbol"] = "300001"
    out, warnings = derive_limits(bars, inst, cal)
    assert out["limit_exempt"].all()
    assert any("无涨跌幅限制" in w for w in warnings)


def test_drop_nonmonotonic_k_removes_reset_symbols():
    """复权因子被数据源重置的标的必须剔除（否则凭空暴跌）。

    实测 gm 在 2018-12-26 把 000022 的 adj_factor 从 3.5265 写成 1.0，
    当日价格没跳空——不剔除会让该股当日凭空跌 71%。
    """
    from a_share_quant.data.builders import drop_nonmonotonic_k

    dates = pd.bdate_range("2024-01-02", periods=4)
    rows = []
    for d in dates:
        rows.append({"trade_date": d, "symbol": "GOOD", "close": 10.0,
                     "adj_close": 30.0})      # k 恒为 3，正常
    for i, d in enumerate(dates):
        k = 3.5265 if i < 2 else 1.0            # k 在第 3 天被重置
        rows.append({"trade_date": d, "symbol": "BAD", "close": 10.0,
                     "adj_close": 10.0 * k})
    bars = pd.DataFrame(rows)

    kept, dropped = drop_nonmonotonic_k(bars)
    assert dropped == ["BAD"]
    assert set(kept["symbol"]) == {"GOOD"}


def test_drop_nonmonotonic_k_keeps_clean_data():
    from a_share_quant.data.builders import drop_nonmonotonic_k

    dates = pd.bdate_range("2024-01-02", periods=3)
    bars = pd.DataFrame([{"trade_date": d, "symbol": "A", "close": 10.0,
                          "adj_close": 10.0 * (2.0 ** (i / 2))}
                         for i, d in enumerate(dates)])
    kept, dropped = drop_nonmonotonic_k(bars)
    assert dropped == []
    assert len(kept) == len(bars)
