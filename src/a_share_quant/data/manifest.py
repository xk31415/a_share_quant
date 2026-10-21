"""数据集指纹：把"这份数据是哪份数据"变成可校验的几 KB 元信息。

**为什么数据本体不入库**

1. 体积：三份数据集共 366 MB，`bars.parquet` 单文件 166 MB，超过 GitHub
   100 MB 硬限制（`git push` 会直接拒绝），只能上 LFS，而 LFS 免费额度
   1 GB 存储 + 1 GB/月流量，别人 clone 三次就用完；
2. 数据每天在长：`update_dataset.py` 补数据时 parquet 是**整体重写**，
   每次提交都是一个新的 66 MB blob，一旦进历史就再也删不掉；
3. 授权：数据来自掘金/baostock 的账号 token，公开转分发通常不在授权范围内。

**替代方案**

仓库只存 `data/MANIFEST.json`。别人用自己的账号重建一份，再跑
`python scripts/data_manifest.py verify`，逐表逐列比对指纹。

这比"我发一份数据给你"更强：它验证的是**"你重建出来的和我回测用的逐字段相同"**，
而不只是"我拿到了某个文件"。文件字节会被 pyarrow 版本影响（parquet 头里带
`created_by`、压缩库版本），所以这里哈希的是**规范化后的内容**：

  * 行按 (trade_date, symbol) 等主键排序（行序不属于内容）；
  * 浮点按 float64 原始位模式哈希，NaN 先抹平（NaN 的 payload 位模式不唯一）；
  * 列名与列顺序参与哈希（schema 变了就应该被发现）；
  * 每列单独一个指纹，比对失败时能直接说出**是哪几列**变了。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

# 指纹格式版本：改了归一化规则就 +1，旧 manifest 会被判为"格式不认识"
MANIFEST_VERSION = 1

# 数据集的表清单（基准单独成表，不混进 bars）
BAR_TABLE = "bars.parquet"
INSTRUMENT_TABLE = "instruments.parquet"
STATUS_TABLE = "status.parquet"
CALENDAR_TABLE = "calendar.csv"
BENCHMARK_TABLE = "benchmark.csv"

TABLE_FILES: tuple[str, ...] = (
    BAR_TABLE, INSTRUMENT_TABLE, STATUS_TABLE, CALENDAR_TABLE, BENCHMARK_TABLE,
)

# 项目里的三份真实数据集（脚本默认处理这三份）
DATASETS: tuple[dict[str, str], ...] = (
    {
        "key": "processed",
        "dir": "data/processed",
        "label": "2020-2024（样本内）",
        "build_command": (
            "python scripts/build_dataset.py --source gm --start 2020-01-01 "
            "--end 2024-12-31 --benchmark 000852 --out data/processed"
        ),
    },
    {
        "key": "processed_2015_2019",
        "dir": "data/processed_2015_2019",
        "label": "2015-2019（样本外 1）",
        "build_command": (
            "python scripts/build_dataset.py --source gm --start 2015-01-01 "
            "--end 2019-12-31 --benchmark 000852 --out data/processed_2015_2019"
        ),
    },
    {
        "key": "processed_2025_2026",
        "dir": "data/processed_2025_2026",
        "label": "2025-01 ~ 2026-10（样本外 2，每日增量更新）",
        "build_command": (
            "python scripts/build_dataset.py --source gm --start 2025-01-01 "
            "--end 2026-10-20 --benchmark 000852 --out data/processed_2025_2026\n"
            "# 之后每天：python scripts/update_dataset.py "
            "--data-dir data/processed_2025_2026"
        ),
    },
)

DEFAULT_MANIFEST_PATH = "data/MANIFEST.json"


# ---------------------------------------------------------------- 归一化
def _sort_rows(df: pd.DataFrame) -> pd.DataFrame:
    """按主键排序，让行序不参与指纹。"""
    for keys in (("trade_date", "symbol"), ("symbol",), ("trade_date",)):
        if all(k in df.columns for k in keys):
            return df.sort_values(by=list(keys), kind="mergesort", na_position="last")
    try:
        return df.sort_values(by=list(df.columns), kind="mergesort", na_position="last")
    except TypeError:          # 混合类型列无法比较，退回原序（指纹仍然可比，只是依赖行序）
        return df


def _column_bytes(s: pd.Series) -> bytes:
    """把一列归一成可比较的字节串。

    只依赖**逻辑值**，不依赖 pyarrow/pandas 版本，所以同一份数据在不同机器上
    重建出来的指纹相同。
    """
    try:
        if pd.api.types.is_datetime64_any_dtype(s):
            return s.to_numpy(dtype="datetime64[ns]").view("i8").tobytes()
        if pd.api.types.is_bool_dtype(s):
            return s.to_numpy(dtype="uint8").tobytes()
        if pd.api.types.is_integer_dtype(s) and not s.isna().any():
            return s.to_numpy(dtype="int64").tobytes()
        if pd.api.types.is_float_dtype(s):
            a = s.to_numpy(dtype="float64", copy=True)
            nan = pd.isna(a)
            a[nan] = 0.0        # NaN 的位模式不唯一，先抹平，用掩码单独记
            a[a == 0.0] = 0.0   # -0.0 与 +0.0 数值相等、字节不等
            return a.tobytes() + nan.tobytes()
    except (TypeError, ValueError):
        pass
    # object / string：必须区分 None 与 ""，否则两种不同的缺失表示会被当成同一份
    vals = s.astype("object").where(~s.isna(), None)
    return b"\x00".join(
        b"\x01NONE" if v is None else str(v).encode("utf-8") for v in vals
    )


def frame_digest(df: pd.DataFrame) -> str:
    """整表内容指纹（与行序、文件字节无关）。"""
    return _frame_digest_sorted(_sort_rows(df))


def _frame_digest_sorted(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(f"v{MANIFEST_VERSION}|{len(df)}|".encode())
    h.update(",".join(map(str, df.columns)).encode())
    h.update(b"|")
    for name in df.columns:
        col = _column_bytes(df[name])
        h.update(f"\x1e{name}:{len(col)}:".encode())
        h.update(col)
    return h.hexdigest()


def column_digests(df: pd.DataFrame) -> dict[str, str]:
    """逐列指纹：比对失败时用来定位"是哪几列变了"。"""
    return _column_digests_sorted(_sort_rows(df))


def _column_digests_sorted(df: pd.DataFrame) -> dict[str, str]:
    out = {}
    for name in df.columns:
        h = hashlib.sha256()
        h.update(f"v{MANIFEST_VERSION}|{name}|{len(df)}|".encode())
        h.update(_column_bytes(df[name]))
        out[str(name)] = h.hexdigest()
    return out


# ---------------------------------------------------------------- 读写
def load_table(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def describe_table(path: Path) -> dict[str, Any]:
    df = _sort_rows(load_table(path))       # 只排一次序，两个指纹复用
    return {
        "file": path.name,
        "rows": int(len(df)),
        "columns": [str(c) for c in df.columns],
        "content_sha256": _frame_digest_sorted(df),
        "column_sha256": _column_digests_sorted(df),
        "size_bytes": int(path.stat().st_size),
    }


def describe_dataset(root: Path, **extra: Any) -> dict[str, Any]:
    """扫描一个数据目录，产出它的指纹条目。

    `size_bytes` 与 parquet 是否换了 pyarrow 版本有关，只作参考，
    **不参与校验**；校验只认 `content_sha256` 与 `rows`。
    """
    data_dir = Path(root)
    files: dict[str, Any] = {}
    for name in TABLE_FILES:
        p = data_dir / name
        if p.exists():
            files[name] = describe_table(p)
    if BAR_TABLE not in files:
        raise SystemExit(f"{data_dir} 里没有 {BAR_TABLE}，不是可用的数据集目录")

    bars = load_table(data_dir / BAR_TABLE)
    dates = pd.to_datetime(bars["trade_date"])
    entry: dict[str, Any] = {
        "files": files,
        "summary": {
            "bars_rows": int(len(bars)),
            "n_symbols": int(bars["symbol"].nunique()) if "symbol" in bars else None,
            "first_date": str(dates.min().date()),
            "last_date": str(dates.max().date()),
            "n_trade_days": int(dates.nunique()),
        },
        "rows_total": int(sum(f["rows"] for f in files.values())),
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    # 汇总指纹：把各表的内容指纹按文件名排序再哈希一次
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(f"{name}:{files[name]['content_sha256']};".encode())
    entry["dataset_sha256"] = h.hexdigest()
    return entry


def load_manifest(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"找不到 {path}：先跑 python scripts/data_manifest.py write")
    payload = json.loads(path.read_text(encoding="utf-8"))
    got = payload.get("version")
    if got != MANIFEST_VERSION:
        raise SystemExit(
            f"{path} 的格式版本是 {got}，本代码只认 {MANIFEST_VERSION}；"
            "请重新生成（旧 manifest 的指纹口径不同，比对无意义）")
    return payload


def save_manifest(payload: dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload.setdefault("version", MANIFEST_VERSION)
    payload.setdefault("datasets", {})
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def update_manifest(path: Path, key: str, entry: dict[str, Any]) -> dict[str, Any]:
    """写入/替换一个数据集条目（其余条目保持不动）。"""
    payload = (
        json.loads(Path(path).read_text(encoding="utf-8"))
        if Path(path).exists() else {"version": MANIFEST_VERSION, "datasets": {}}
    )
    if payload.get("version") != MANIFEST_VERSION:
        payload = {"version": MANIFEST_VERSION, "datasets": {}}
    payload.setdefault("datasets", {})[key] = entry
    save_manifest(payload, path)
    return payload


# ---------------------------------------------------------------- 校验
def verify_dataset(data_dir: Path, entry: dict[str, Any]) -> list[str]:
    """比对目录与条目，返回问题清单（空 = 通过）。"""
    data_dir = Path(data_dir)
    problems: list[str] = []
    expected = entry.get("files", {})

    for name, meta in expected.items():
        p = data_dir / name
        if not p.exists():
            problems.append(f"缺文件 {name}")
            continue
        got = describe_table(p)
        if got["rows"] != meta["rows"]:
            problems.append(
                f"{name} 行数不同：manifest {meta['rows']:,} vs 实际 {got['rows']:,}")
        if got["content_sha256"] != meta["content_sha256"]:
            changed = sorted(
                c for c in set(got["column_sha256"]) | set(meta["column_sha256"])
                if got["column_sha256"].get(c) != meta["column_sha256"].get(c)
            )
            problems.append(
                f"{name} 内容不同（差异列：{', '.join(changed) or '未知'}）")

    extra = sorted(
        p.name for p in data_dir.glob("*")
        if p.is_file() and p.suffix in {".parquet", ".csv"} and p.name not in expected
    )
    if extra:
        problems.append(
            f"[info] manifest 里没有的额外表：{', '.join(extra)}（不影响校验，仅供参考）")
    return problems


def verify_entry_by_key(manifest_path: Path, key: str,
                        data_dir: Path) -> list[str]:
    payload = load_manifest(manifest_path)
    if key not in payload.get("datasets", {}):
        return [f"manifest 里没有数据集 {key}"]
    return verify_dataset(data_dir, payload["datasets"][key])


def describe_manifest_entry(entry: dict[str, Any]) -> str:
    s = entry.get("summary", {})
    first = s.get("first_date") or "?"
    last = s.get("last_date") or "?"
    return (
        f"{s.get('bars_rows', 0):>9,} 行  {s.get('n_symbols') or 0:>5} 只  "
        f"{first} ~ {last}  {s.get('n_trade_days', 0)} 交易日  "
        f"指纹 {str(entry.get('dataset_sha256'))[:12]}"
    )
