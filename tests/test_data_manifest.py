"""数据集指纹的稳定性与敏感度。

指纹用来替代"把数据放进仓库"：别人重建一份后，靠它判断是否与本仓库
回测用的是同一份。所以它必须满足两个方向的要求——

  * 不该变的不能变：行序、pyarrow 版本、-0.0/0.0 这种数值等价的表示；
  * 该变的必须变：缺一天、某个价格改了、某列类型变了。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from a_share_quant.data.manifest import (
    MANIFEST_VERSION,
    describe_dataset,
    describe_table,
    frame_digest,
    load_manifest,
    load_table,
    update_manifest,
    verify_dataset,
)


def _bars() -> pd.DataFrame:
    return pd.DataFrame({
        "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-02"]),
        "symbol": ["000001", "000001", "600000"],
        "open": [10.0, 10.5, 8.0],
        "close": [10.5, 10.2, 7.9],
        "volume": [1000.0, 1200.0, 900.0],
        "is_st": [False, False, True],
        "industry": ["银行", "银行", None],
    })


# ---------------------------------------------------------------- 稳定性
def test_digest_ignores_row_order():
    df = _bars()
    assert frame_digest(df) == frame_digest(df.sample(frac=1.0, random_state=7))


def test_digest_ignores_negative_zero():
    """-0.0 与 0.0 数值相等，不能因为浮点位模式不同被判成两份数据。"""
    a = pd.DataFrame({"x": [0.0, 1.0]})
    b = pd.DataFrame({"x": [-0.0, 1.0]})
    assert frame_digest(a) == frame_digest(b)


def test_digest_survives_parquet_roundtrip(tmp_path):
    """NaN 经 parquet 往返后位模式可能不同，指纹不能因此变化。"""
    df = pd.DataFrame({"x": [float("nan"), 1.0], "s": [None, "银行"]})
    p = tmp_path / "t.parquet"
    df.to_parquet(p, index=False)
    assert frame_digest(df) == frame_digest(pd.read_parquet(p))


# ---------------------------------------------------------------- 敏感度
def test_digest_detects_changed_value():
    df = _bars()
    other = df.copy()
    other.loc[0, "close"] = 10.5000001      # 只动第九位小数也要发现
    assert frame_digest(df) != frame_digest(other)


def test_digest_detects_missing_row():
    df = _bars()
    assert frame_digest(df) != frame_digest(df.iloc[:-1])


def test_digest_distinguishes_none_from_empty_string():
    """None 与 "" 是两种缺失，不能被哈希成同一份。"""
    a = pd.DataFrame({"industry": [None, "银行"]})
    b = pd.DataFrame({"industry": ["", "银行"]})
    assert frame_digest(a) != frame_digest(b)


def test_digest_detects_dtype_change():
    """float 1.0 与 int 1 数值相同但类型不同——数据源换字段类型要能发现。"""
    a = pd.DataFrame({"volume": [1.0, 2.0]})
    b = pd.DataFrame({"volume": [1, 2]})
    assert frame_digest(a) != frame_digest(b)


def test_digest_detects_column_reorder():
    a = pd.DataFrame({"x": [1.0], "y": [2.0]})
    b = pd.DataFrame({"y": [2.0], "x": [1.0]})
    assert frame_digest(a) != frame_digest(b)


# ---------------------------------------------------------------- 目录级
def _write_dataset(root, bars: pd.DataFrame) -> None:
    root.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(root / "bars.parquet", index=False)
    pd.DataFrame({
        "symbol": ["000001", "600000"],
        "name": ["平安银行", "浦发银行"],
        "list_date": pd.to_datetime(["1991-04-03", "1999-11-10"]),
    }).to_parquet(root / "instruments.parquet", index=False)
    pd.DataFrame({
        "trade_date": pd.to_datetime(["2024-01-02", "2024-01-02"]),
        "symbol": ["000001", "600000"],
        "is_st": [False, True],
        "is_suspended": [False, False],
    }).to_parquet(root / "status.parquet", index=False)
    pd.DataFrame({"trade_date": ["2024-01-02", "2024-01-03"]}).to_csv(
        root / "calendar.csv", index=False)


def test_describe_and_verify_roundtrip(tmp_path):
    d = tmp_path / "ds"
    _write_dataset(d, _bars())
    entry = describe_dataset(d)
    assert entry["summary"]["bars_rows"] == 3
    assert entry["summary"]["n_symbols"] == 2
    assert verify_dataset(d, entry) == []


def test_verify_ignores_file_size_drift(tmp_path):
    """换了 pyarrow 版本重写同一份数据，文件字节变了但内容一致，必须判通过。"""
    d = tmp_path / "ds"
    _write_dataset(d, _bars())
    entry = describe_dataset(d)
    entry["files"]["bars.parquet"]["size_bytes"] = 12345      # 只改参考字段
    assert verify_dataset(d, entry) == []


def test_verify_names_the_changed_column(tmp_path):
    d = tmp_path / "ds"
    _write_dataset(d, _bars())
    entry = describe_dataset(d)
    bars = _bars()
    bars.loc[0, "close"] = 99.0
    bars.to_parquet(d / "bars.parquet", index=False)

    problems = verify_dataset(d, entry)
    assert len(problems) == 1
    assert "close" in problems[0]
    assert "open" not in problems[0]        # 只报真正变了的列


def test_verify_reports_missing_file(tmp_path):
    d = tmp_path / "ds"
    _write_dataset(d, _bars())
    entry = describe_dataset(d)
    (d / "status.parquet").unlink()
    problems = verify_dataset(d, entry)
    assert any("status.parquet" in p and "缺文件" in p for p in problems)


def test_verify_flags_row_count_change(tmp_path):
    d = tmp_path / "ds"
    _write_dataset(d, _bars())
    entry = describe_dataset(d)
    _bars().iloc[:-1].to_parquet(d / "bars.parquet", index=False)
    problems = verify_dataset(d, entry)
    assert any("行数不同" in p for p in problems)


# ---------------------------------------------------------------- manifest 文件
def test_update_manifest_keeps_other_entries(tmp_path):
    d = tmp_path / "ds"
    _write_dataset(d, _bars())
    path = tmp_path / "MANIFEST.json"
    update_manifest(path, "a", describe_dataset(d))
    update_manifest(path, "b", describe_dataset(d))
    payload = load_manifest(path)
    assert set(payload["datasets"]) == {"a", "b"}
    assert payload["version"] == MANIFEST_VERSION


def test_load_manifest_rejects_old_version(tmp_path):
    path = tmp_path / "MANIFEST.json"
    path.write_text(json.dumps({"version": 0, "datasets": {}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_manifest(path)


def test_describe_table_has_per_column_digests(tmp_path):
    d = tmp_path / "ds"
    _write_dataset(d, _bars())
    meta = describe_table(d / "bars.parquet")
    assert set(meta["column_sha256"]) == set(_bars().columns)


def test_load_table_keeps_leading_zeros(tmp_path):
    """CSV 里的 000852 不能读成 852（R05 F-22）。

    读成整数后，一份丢掉前导零的 benchmark.csv 与正确文件指纹相同，
    校验会放行——而下游按字符串 "000852" 对齐基准会对不上，超额又可以指错序列。
    """
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    pd.DataFrame({"trade_date": ["2024-01-02"], "symbol": ["000852"],
                  "close": [1.0]}).to_csv(a, index=False)
    pd.DataFrame({"trade_date": ["2024-01-02"], "symbol": ["852"],
                  "close": [1.0]}).to_csv(b, index=False)
    assert load_table(a)["symbol"].iloc[0] == "000852"
    assert frame_digest(load_table(a)) != frame_digest(load_table(b))
