"""成交对账测试。

对账的价值全在"能抓到异常"：如果它把超卖、超量、对不上单都算进统计里，
那这份报告比没有更危险——它会让人以为执行正常。
"""
from __future__ import annotations

import pandas as pd
import pytest

from a_share_quant.live.reconcile import (
    ReconcileError,
    aggregate_fills,
    apply_fills,
    compare_holdings,
    match_orders,
    normalize_fills,
    normalize_side,
    summarize,
)


def _orders(rows):
    return pd.DataFrame(rows, columns=["symbol", "side", "shares", "price"])


def _fills(rows, with_fee=True):
    cols = ["symbol", "side", "shares", "price"] + (["fee"] if with_fee else [])
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------- 规范化
def test_normalize_side_accepts_broker_wording():
    for raw, want in (("buy", "buy"), ("BUY", "buy"), ("买入", "buy"),
                      ("证券买入", "buy"), ("sell", "sell"), ("卖出", "sell"),
                      ("证券卖出", "sell")):
        assert normalize_side(raw) == want


def test_normalize_side_rejects_unknown():
    with pytest.raises(ReconcileError, match="无法识别"):
        normalize_side("调整")


def test_normalize_fills_requires_columns():
    with pytest.raises(ReconcileError, match="缺列"):
        normalize_fills(pd.DataFrame({"symbol": ["A"], "side": ["buy"]}))


def test_normalize_fills_drops_zero_and_coerces():
    raw = _fills([("A", "买入", "100", "10.5", "5"),
                  ("B", "卖出", 0, 9.0, 5)])
    out = normalize_fills(raw)
    assert len(out) == 1
    assert out.iloc[0]["side"] == "buy"
    assert out.iloc[0]["shares"] == 100


def test_aggregate_fills_weights_price_by_shares():
    f = _fills([("A", "buy", 100, 10.0, 5), ("A", "buy", 300, 11.0, 5)])
    agg = aggregate_fills(normalize_fills(f))
    assert len(agg) == 1
    assert agg.iloc[0]["shares"] == 400
    assert agg.iloc[0]["price"] == pytest.approx((100 * 10 + 300 * 11) / 400)
    assert agg.iloc[0]["fee"] == 10


# ---------------------------------------------------------------- 匹配
def test_match_orders_status_all_four_kinds():
    orders = _orders([("A", "buy", 1000, 10.0), ("B", "buy", 1000, 20.0),
                      ("C", "sell", 500, 5.0), ("D", "buy", 300, 8.0)])
    fills = normalize_fills(_fills([("A", "buy", 1000, 10.1, 5),
                                    ("B", "buy", 400, 20.2, 8),
                                    ("D", "buy", 300, 8.0, 5)]))
    matched, unmatched = match_orders(orders, fills)
    st = dict(zip(matched["symbol"], matched["status"]))
    assert st == {"A": "filled", "B": "partial", "C": "missing", "D": "filled"}
    assert len(unmatched) == 0


def test_match_orders_flags_over_fill():
    """成交多于委托必须单列——可能是手动补单，不能混进正常统计。"""
    matched, _ = match_orders(_orders([("A", "buy", 500, 10.0)]),
                              normalize_fills(_fills([("A", "buy", 800, 10.0, 5)])))
    assert matched.iloc[0]["status"] == "over"
    assert matched.iloc[0]["shares_gap"] == 300


def test_match_orders_splits_unmatched_fills():
    matched, unmatched = match_orders(
        _orders([("A", "buy", 100, 10.0)]),
        normalize_fills(_fills([("A", "buy", 100, 10.0, 5),
                                ("Z", "sell", 200, 3.0, 5)])))
    assert len(matched) == 1
    assert len(unmatched) == 1 and unmatched.iloc[0]["symbol"] == "Z"


def test_match_orders_ignores_zero_share_orders():
    """orders.csv 里 0 股的记录是"无法成交"，不参与对账。"""
    matched, _ = match_orders(_orders([("A", "buy", 0, 10.0)]), normalize_fills(_fills([])))
    assert matched.empty


def test_slippage_sign_is_unfavourable_positive():
    orders = _orders([("A", "buy", 100, 10.0), ("B", "sell", 100, 10.0)])
    fills = normalize_fills(_fills([("A", "buy", 100, 10.05, 5),
                                    ("B", "sell", 100, 9.95, 5)]))
    matched, _ = match_orders(orders, fills)
    slip = dict(zip(matched["symbol"], matched["slip_bps"]))
    assert slip["A"] == pytest.approx(50.0, abs=1e-6), "买贵 0.5% 应记 +50bp"
    assert slip["B"] == pytest.approx(50.0, abs=1e-6), "卖便宜 0.5% 也应记 +50bp"


def test_slippage_negative_when_price_improves():
    matched, _ = match_orders(_orders([("A", "buy", 100, 10.0)]),
                              normalize_fills(_fills([("A", "buy", 100, 9.9, 5)])))
    assert matched.iloc[0]["slip_bps"] == pytest.approx(-100.0, abs=1e-6)


# ---------------------------------------------------------------- 持仓
def test_apply_fills_adds_and_clears():
    pos, warn = apply_fills({"A": 1000, "B": 500},
                            normalize_fills(_fills([("A", "buy", 200, 10.0, 5),
                                                    ("B", "sell", 500, 9.0, 5)])))
    assert pos == {"A": 1200}
    assert warn == [], "整手买卖且清仓，不应有告警"


def test_apply_fills_warns_on_oversell():
    pos, warn = apply_fills({"A": 100},
                            normalize_fills(_fills([("A", "sell", 300, 10.0, 5)])))
    assert "A" not in pos
    assert any("超过持仓" in w for w in warn)


def test_apply_fills_warns_on_odd_lot_partial_sell():
    """卖出 150 股、还剩 50 股：违反整手规则（只有清仓能带零股）。"""
    pos, warn = apply_fills({"A": 200},
                            normalize_fills(_fills([("A", "sell", 150, 10.0, 5)])))
    assert pos["A"] == 50
    assert any("未清仓" in w for w in warn)


def test_apply_fills_allows_odd_lot_clearance():
    pos, warn = apply_fills({"A": 150},
                            normalize_fills(_fills([("A", "sell", 150, 10.0, 5)])))
    assert pos == {}
    assert warn == []


def test_apply_fills_warns_on_odd_lot_buy():
    _, warn = apply_fills({}, normalize_fills(_fills([("A", "buy", 150, 10.0, 5)])))
    assert any("整数倍" in w for w in warn)


# ---------------------------------------------------------------- 偏差
def test_compare_holdings_marks_material_gap():
    target = pd.DataFrame({"symbol": ["A", "B"], "target_shares": [1000, 500],
                           "price": [10.0, 20.0]})
    gap = compare_holdings({"A": 900, "B": 500}, target, lot_size=100)
    a = gap[gap["symbol"] == "A"].iloc[0]
    b = gap[gap["symbol"] == "B"].iloc[0]
    assert a["shares_gap"] == -100 and a["material"] and a["value_gap"] == -1000
    assert b["shares_gap"] == 0 and not b["material"]


def test_compare_holdings_includes_extra_position():
    target = pd.DataFrame({"symbol": ["A"], "target_shares": [100], "price": [10.0]})
    gap = compare_holdings({"A": 100, "Z": 300}, target, lot_size=100)
    z = gap[gap["symbol"] == "Z"].iloc[0]
    assert z["target_shares"] == 0 and z["actual_shares"] == 300


def test_compare_holdings_requires_target_columns():
    with pytest.raises(ReconcileError, match="缺列"):
        compare_holdings({}, pd.DataFrame({"symbol": ["A"]}))


# ---------------------------------------------------------------- 汇总
def test_summarize_counts_and_ratios():
    orders = _orders([("A", "buy", 1000, 10.0), ("B", "buy", 1000, 20.0),
                      ("C", "sell", 500, 5.0)])
    fills = normalize_fills(_fills([("A", "buy", 1000, 10.0, 5),
                                    ("B", "buy", 500, 20.0, 5)]))
    matched, unmatched = match_orders(orders, fills)
    s = summarize(matched, unmatched)
    assert s["n_orders"] == 3 and s["n_filled"] == 1
    assert s["n_partial"] == 1 and s["n_missing"] == 1
    assert s["fill_ratio"] == pytest.approx(1500 / 2500)
    assert s["total_fee"] == 10


def test_summarize_handles_empty_matched():
    s = summarize(pd.DataFrame(), pd.DataFrame())
    assert s["n_orders"] == 0 and s["fill_ratio"] == 0.0


def test_summarize_includes_fee_from_unmatched_fills():
    """无对应委托的成交也是真花钱，不能从总费用里漏掉。"""
    orders = _orders([("A", "buy", 100, 10.0)])
    fills = normalize_fills(_fills([("A", "buy", 100, 10.0, 5),
                                    ("Z", "buy", 100, 20.0, 7)]))
    matched, unmatched = match_orders(orders, fills)
    s = summarize(matched, unmatched)
    assert s["total_fee"] == 12
    assert s["n_unmatched_fills"] == 1
