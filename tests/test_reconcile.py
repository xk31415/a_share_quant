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


# ---------------------------------------------------------------- R06 F-31
def test_summarize_never_emits_nan_ratios():
    """R06 F-31：完全未成交时比率必须是 0，不能是 NaN。

    函数说明写的是"分母为 0 时返回 0，不返回 NaN 混进报告"，实现原来相反。
    NaN 会被 json.dumps 写成非标准字面量，严格 JSON 解析器读不了。
    """
    orders = _orders([("A", "buy", 100, 10.0)])
    fills = normalize_fills(_fills([], with_fee=True))
    matched, unmatched = match_orders(orders, fills)
    s = summarize(matched, unmatched)
    assert s["n_filled"] == 0 and s["fill_ratio"] == 0.0
    assert s["fee_bps"] == 0.0 and s["weighted_slip_bps"] == 0.0
    # 必须真的不是 NaN，而不是"碰巧等于某个东西"
    assert s["fee_bps"] == s["fee_bps"]
    assert s["weighted_slip_bps"] == s["weighted_slip_bps"]


def test_summarize_json_is_strict_parseable_when_nothing_filled():
    """同上，但走到产出那一步：文件必须能被**严格** JSON 解析器读。

    Python 的 json 默认容忍 NaN，所以只在跨语言读这个文件时才会炸。
    """
    import json

    def _reject(x):
        raise ValueError(f"非标准 JSON 常量: {x}")

    orders = _orders([("A", "buy", 100, 10.0)])
    matched, unmatched = match_orders(orders, normalize_fills(_fills([])))
    text = json.dumps(summarize(matched, unmatched), ensure_ascii=False,
                      default=float)
    assert json.loads(text, parse_constant=_reject)  # 不抛异常即通过


def test_fills_digest_ignores_row_order_but_catches_new_fills():
    """R06 F-26 的幂等保护：同一批成交换个行序必须认出来是同一批。

    用文件字节或原始行序做指纹的话，重排成交顺序就会被判成"另一批成交"，
    重复入账的保护形同虚设。
    """
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "scripts" / "reconcile.py"
    spec = importlib.util.spec_from_file_location("reconcile_script", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    a = mod._fills_digest(normalize_fills(_fills(
        [("A", "buy", 100, 10.0, 5), ("B", "sell", 200, 20.0, 8)])))
    b = mod._fills_digest(normalize_fills(_fills(
        [("B", "sell", 200, 20.0, 8), ("A", "buy", 100, 10.0, 5)])))
    c = mod._fills_digest(normalize_fills(_fills(
        [("A", "buy", 101, 10.0, 5), ("B", "sell", 200, 20.0, 8)])))
    assert a == b, "重排成交顺序不该被判成另一批成交"
    assert a != c, "改一股也必须认出来是新的一批"


# ---------------------------------------------------------------- R07 F-32/F-33
def _load_script():
    """按文件路径加载 scripts/reconcile.py（它在 scripts/ 下，不是可导入包）。"""
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "scripts" / "reconcile.py"
    spec = importlib.util.spec_from_file_location("reconcile_script", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _digest_of(rows, **kw):
    mod = _load_script()
    return mod._fills_digest(normalize_fills(_fills(rows, **kw)))


def test_digest_survives_a_different_broker_export_template():
    """R07 F-32：同一批成交换一种导出模板，指纹必须不变。

    原实现拿整个 DataFrame 去哈希，于是列顺序、多余列（成交编号）、
    有没有 fee 列、dtype（`10` vs `10.0`）、明细还是汇总都会改变指纹——
    当天用「当日成交」对一遍、次日拿「交割单」再对一遍，保护全部失效，
    股数被加第二遍。这里守住"指纹只取决于成交内容本身"。
    """
    base = [("A", "buy", 100, 10.0, 5.0), ("B", "sell", 200, 20.0, 8.0)]
    ref = _digest_of(base)

    # (a) 明细 vs 汇总：A 拆成 60 + 40，加权价仍是 10.0
    assert _digest_of([("A", "buy", 60, 10.0, 3.0),
                       ("A", "buy", 40, 10.0, 2.0),
                       ("B", "sell", 200, 20.0, 8.0)]) == ref
    # (c) 没有 fee 列（客户端「当日成交」导出的常见形态）
    assert _digest_of([("A", "buy", 100, 10.0), ("B", "sell", 200, 20.0)],
                      with_fee=False) == ref


def test_digest_ignores_column_layout_and_dtype():
    """同上，但直接构造不同布局的 DataFrame（列顺序、多余列、整数 dtype）。

    `_fills` helper 造不出这些形态，只能在 DataFrame 层面构造。
    """
    mod = _load_script()
    ref = _digest_of([("A", "buy", 100, 10.0, 5.0), ("B", "sell", 200, 20.0, 8.0)])

    def dig(df):
        return mod._fills_digest(normalize_fills(df))

    std = normalize_fills(_fills([("A", "buy", 100, 10.0, 5.0),
                                  ("B", "sell", 200, 20.0, 8.0)]))
    # (a) 列顺序不同
    assert dig(std[["side", "symbol", "price", "shares", "fee"]]) == ref
    # (b) 多一列无关字段（成交编号）
    extra = std.copy()
    extra["trade_id"] = ["T001", "T002"]
    assert dig(extra) == ref
    # (d)(e) 整列整数 dtype：10 vs 10.0、5 vs 5.0
    ints = pd.DataFrame({"symbol": ["A", "B"], "side": ["buy", "sell"],
                         "shares": [100, 200], "price": [10, 20], "fee": [5, 8]})
    assert dig(ints) == ref
    # 仍然要认得出"改了一股"是新的一批
    assert _digest_of([("A", "buy", 101, 10.0, 5.0),
                       ("B", "sell", 200, 20.0, 8.0)]) != ref


def test_digest_does_not_depend_on_the_date_or_on_empty_batches():
    """R07 F-33 + R08 F-37：日期不参与指纹，空批次不产生指纹。

    R07 用「日期 + 指纹」当判定键来修跨天误伤，结果**日期本身成了绕过手段**：
    改一下 `--as-of` 就能把同一份成交连加三次（1000 → 1100 → 1200 → 1300）。
    现在指纹只由股数决定，日期只写进历史供人看；空批次返回空串、不记历史，
    于是连续两天空成交不会互相误伤。
    """
    import inspect

    mod = _load_script()
    assert mod._fills_digest(normalize_fills(_fills([]))) == "", \
        "空批次不应产生指纹"
    # 指纹函数的签名里根本没有日期——换 --as-of 不可能改变它
    assert "as_of" not in inspect.signature(mod._fills_digest).parameters
    one = normalize_fills(_fills([("A", "buy", 100, 10.0, 5.0)]))
    again = normalize_fills(_fills([("A", "buy", 100, 10.0, 5.0)]))
    assert mod._fills_digest(one) == mod._fills_digest(again)


def test_digest_ignores_price_to_two_decimals():
    """R08 F-36：明细价加权后与券商两位小数汇总价，指纹必须相同。

    两笔 100 股 @10.01 与 @10.02 加权是 10.015，`round(3)` 记 10.015；
    券商「交割单」按两位小数写一行 `200 @ 10.02`，指纹对不上，股数被再加一遍
    （实测 1200 → 1400）。价格干脆不进指纹——决定持仓变化的硬信息只有股数。
    """
    detail = _digest_of([("A", "buy", 100, 10.01, 5.0),
                         ("A", "buy", 100, 10.02, 5.0)])
    summary = _digest_of([("A", "buy", 200, 10.02, 10.0)])
    exact = _digest_of([("A", "buy", 200, 10.015, 10.0)])
    assert detail == summary, "两位小数的汇总价必须认得出是同一批"
    assert detail == exact


def test_digest_history_blocks_any_earlier_batch_not_just_the_last(tmp_path):
    """R08：判定看的是**历史**，不是"最后一批"。

    只记最后一批时，`h0 -A→ h1 -B→ h2` 之后拿 h2 重跑 A 不拦——A 的股数
    早就在 h2 里了。历史列表把这条补上。
    """
    mod = _load_script()
    p = tmp_path / "holdings.digest"
    mod._append_digest(p, "2026-10-20", "aaaa")
    mod._append_digest(p, "2026-10-21", "bbbb")
    hist = mod._digest_history(p)
    assert "aaaa" in hist, "旧批次不能被最后一批顶掉"
    assert "bbbb" in hist
    assert "cccc" not in hist


def test_infer_as_of_reads_the_date_directory_name():
    """同上：日期默认从委托单所在目录名取（`live/<日期>/`）。"""
    from pathlib import Path

    mod = _load_script()
    assert mod._infer_as_of(Path("live/2026-10-20")) == "2026-10-20"
    # 推断不出就用 unknown：宁可拦错，不可漏拦；真要重跑用 --force
    assert mod._infer_as_of(Path("live")) == "unknown"


# ---------------------------------------------------------------- R07 F-34/F-35
def _run_cli(tmp_path, orders_rows, fills_rows, holdings, target_rows=None,
             extra=(), save_name="out.csv", day_name="day",
             holdings_path=None):
    """跑一次 scripts/reconcile.py，返回 (退出码, 输出目录, 新持仓路径)。

    链式跑法（h0 → h1 → h2）必须把上一次的**输出路径**直接当这一次的
    `--holdings`：指纹挂在输入持仓的文件名旁边，若每次都另建一个输入文件，
    第二次就查不到第一次写的指纹，保护形同虚设——这正是 R06 F-26 第一版
    把指纹挂在输出侧时犯的同一个错。
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    day = tmp_path / day_name
    day.mkdir(parents=True, exist_ok=True)
    (day / "orders.csv").write_text(
        "symbol,side,shares,price\n" + "".join(
            f"{s},{d},{n},{p}\n" for s, d, n, p in orders_rows), encoding="utf-8")
    if target_rows is not None:
        (day / "target.csv").write_text(
            "symbol,target_shares,price\n" + "".join(
                f"{s},{n},{p}\n" for s, n, p in target_rows), encoding="utf-8")
    f = tmp_path / "fills.csv"
    f.write_text("symbol,side,shares,price,fee\n" + "".join(
        f"{s},{d},{n},{p},{fee}\n" for s, d, n, p, fee in fills_rows),
        encoding="utf-8")
    if holdings_path is not None:
        h = Path(holdings_path)
    else:
        h = tmp_path / "holdings_in.csv"
        h.write_text("symbol,shares\n" + "".join(
            f"{s},{n}\n" for s, n in holdings), encoding="utf-8")
    out = tmp_path / save_name
    r = subprocess.run(
        [sys.executable, str(root / "scripts" / "reconcile.py"),
         "--orders", str(day), "--fills", str(f), "--holdings", str(h),
         "--save-holdings", str(out), *extra],
        capture_output=True, text=True, encoding="utf-8", cwd=str(root))
    return r.returncode, day, out


def test_blocked_rerun_leaves_the_first_report_intact(tmp_path):
    """R07 F-34：被指纹拦下的重跑，不能改写 out_dir 下的报告。

    报告写盘原来在拦截之后，于是被拦的重跑会把"按重复入账算出的偏离"
    写进 holdings_gap.csv / reconcile.json，覆盖第一次的正确版本且无法恢复。
    持仓是对的，给人看的报告却变成一份并不存在的偏离。
    """
    import json

    orders = [("A", "buy", 100, 10.0), ("B", "sell", 200, 20.0)]
    fills = [("A", "buy", 100, 10.0, 5.0), ("B", "sell", 200, 20.0, 8.0)]
    target = [("A", 1100, 10.0), ("B", 300, 20.0)]
    holdings = [("A", 1000), ("B", 500)]

    code1, day, out1 = _run_cli(tmp_path, orders, fills, holdings, target)
    assert code1 == 0, "第一次对账应当正常"
    gap1 = (day / "holdings_gap.csv").read_text(encoding="utf-8")
    js1 = (day / "reconcile.json").read_text(encoding="utf-8")
    assert json.loads(js1)["n_holdings_diff"] == 0, "第一次不该有持仓偏离"

    # 同一批成交再跑一次：以 h1 为输入（链式），必须被拦，且报告保持第一次的内容
    code2, _, out2 = _run_cli(tmp_path, orders, fills, None, target,
                              save_name="h2.csv", holdings_path=out1)
    assert code2 == 1, "同一批成交再跑应被指纹拦下"
    assert not out2.exists(), "被拦时不能写持仓"
    assert (day / "holdings_gap.csv").read_text(encoding="utf-8") == gap1
    assert (day / "reconcile.json").read_text(encoding="utf-8") == js1


def test_stale_unmatched_file_is_removed_after_the_fix(tmp_path):
    """R07 F-35：`unmatched_fills.csv` 只写不清，修正后重跑会残留旧文件。

    残留的旧表和 reconcile.json 里 `n_unmatched_fills = 0` 互相矛盾，
    有人会照着它去追查一笔已经排除的成交。
    """
    import json

    orders = [("A", "buy", 100, 10.0)]
    holdings = [("A", 1000)]
    bad = [("A", "buy", 100, 10.0, 5.0), ("C", "buy", 100, 50.0, 5.0)]
    good = [("A", "buy", 100, 10.0, 5.0)]

    code1, day1, _ = _run_cli(tmp_path, orders, bad, holdings)
    assert code1 == 2, "有无对应委托的成交应当报人工处理"
    assert (day1 / "unmatched_fills.csv").exists()

    # 换成修正后的成交表重跑：残留必须被清掉
    code2, day2, _ = _run_cli(tmp_path, orders, good, holdings)
    assert code2 == 0
    assert json.loads((day2 / "reconcile.json").read_text(
        encoding="utf-8"))["n_unmatched_fills"] == 0
    assert not (day2 / "unmatched_fills.csv").exists(), "残留文件应被清理"


# ---------------------------------------------------------------- R08 F-37/F-38/F-39
def _cli(root, day, fills, *extra, holdings=None, save=None):
    """更底层的 CLI 调用：holdings / save 可以显式不传。"""
    import subprocess
    import sys

    cmd = [sys.executable, str(root / "scripts" / "reconcile.py"),
           "--orders", str(day), "--fills", str(fills)]
    if holdings is not None:
        cmd += ["--holdings", str(holdings)]
    if save is not None:
        cmd += ["--save-holdings", str(save)]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", cwd=str(root)).returncode


def test_changing_as_of_does_not_bypass_the_duplicate_guard(tmp_path):
    """R08 F-37：`--as-of` 不能成为绕过重复入账保护的手段。

    R07 把「日期 + 指纹」做进判定键，于是同一份成交只要换一个 `--as-of`
    就能连加：1000 → 1100 → 1200 → 1300，三次退出码全是 0。
    现在日期只写进历史供人看，判定只比对股数指纹。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    day = tmp_path / "2026-10-20"
    day.mkdir()
    (day / "orders.csv").write_text("symbol,side,shares,price\nA,buy,100,10.0\n",
                                    encoding="utf-8")
    fills = tmp_path / "f.csv"
    fills.write_text("symbol,side,shares,price,fee\nA,buy,100,10.0,5.0\n",
                     encoding="utf-8")
    h = tmp_path / "h0.csv"
    h.write_text("symbol,shares\nA,1000\n", encoding="utf-8")

    assert _cli(root, day, fills, holdings=h, save=tmp_path / "h1.csv") == 0
    # 换一个 --as-of，指纹没变，必须拦住
    assert _cli(root, day, fills, "--as-of", "2026-10-21",
                holdings=tmp_path / "h1.csv", save=tmp_path / "h2.csv") == 1
    assert not (tmp_path / "h2.csv").exists()
    # 再换回来也一样
    assert _cli(root, day, fills, holdings=tmp_path / "h1.csv",
                save=tmp_path / "h3.csv") == 1


def test_report_is_not_rewritten_when_holdings_passed_without_saving(tmp_path):
    """R08 F-38：不传 `--save-holdings` 的重跑也必须查指纹，不能改报告。

    原条件 `if args.save_holdings and not fills.empty`，于是「不写持仓、只写报告」
    的那次重跑完全不查指纹：退出码 0，`n_holdings_diff` 0→2、`abs_value_gap` 0→3300，
    第一次的正确报告被按重复入账算出的偏离盖掉。
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    day = tmp_path / "d"
    day.mkdir()
    (day / "orders.csv").write_text(
        "symbol,side,shares,price\nA,buy,100,10.0\nB,sell,200,20.0\n", encoding="utf-8")
    (day / "target.csv").write_text(
        "symbol,target_shares,price\nA,1100,10.0\nB,300,20.0\n", encoding="utf-8")
    fills = tmp_path / "f.csv"
    fills.write_text(
        "symbol,side,shares,price,fee\nA,buy,100,10.0,5.0\nB,sell,200,20.0,8.0\n",
        encoding="utf-8")
    h0 = tmp_path / "h0.csv"
    h0.write_text("symbol,shares\nA,1000\nB,500\n", encoding="utf-8")
    h1 = tmp_path / "h1.csv"

    assert _cli(root, day, fills, holdings=h0, save=h1) == 0
    before = (day / "reconcile.json").read_text(encoding="utf-8")
    assert json.loads(before)["n_holdings_diff"] == 0

    # 不传 --save-holdings 的重跑：必须退出码 1，且报告一字不改
    assert _cli(root, day, fills, holdings=h1) == 1
    assert (day / "reconcile.json").read_text(encoding="utf-8") == before


def test_existing_holdings_file_is_never_overwritten_by_an_empty_start(tmp_path):
    """R08 F-39：`--save-holdings` 指向已存在持仓、又没传 `--holdings` 时必须拒绝。

    按空仓对账会把整份持仓盖成"这一笔成交的结果"。成交全是买入的那天不触发
    超卖保护，退出码 0、标准输出无提示——实测 `A,1000 / B,500` 被盖成 `A,100`，
    1500 股一起没了。次日建仓会按"只有 100 股"重算整批仓位。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    day = tmp_path / "d"
    day.mkdir()
    (day / "orders.csv").write_text("symbol,side,shares,price\nA,buy,100,10.0\n",
                                    encoding="utf-8")
    fills = tmp_path / "f.csv"
    fills.write_text("symbol,side,shares,price,fee\nA,buy,100,10.0,5.0\n",
                     encoding="utf-8")
    book = tmp_path / "book.csv"
    book.write_text("symbol,shares\nA,1000\nB,500\n", encoding="utf-8")
    before = book.read_text(encoding="utf-8")

    # 不传 --holdings：拒绝，文件不变
    assert _cli(root, day, fills, save=book) != 0
    assert book.read_text(encoding="utf-8") == before, "持仓文件不该被覆盖"

    # 显式承认"成交前确实空仓"才放行
    assert _cli(root, day, fills, "--from-empty", save=book) == 0
    assert "A,100" in book.read_text(encoding="utf-8")
