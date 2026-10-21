"""生成 / 校验数据集指纹（data/MANIFEST.json）。

数据本体不入库（体积 366 MB、超过 GitHub 单文件 100 MB 限制、且数据授权
跟账号走），仓库里只放这份几 KB 的指纹。别人用自己的账号重建数据后跑
`verify`，能确认拿到的和本仓库回测用的是同一份。

用法：
    # 生成 / 刷新三份真实数据集的指纹
    python scripts/data_manifest.py write

    # 只处理一份（比如每日更新之后）
    python scripts/data_manifest.py write --data-dir data/processed_2025_2026

    # 校验：内容与指纹是否一致（不一致时退出码 1，可直接挂 CI）
    python scripts/data_manifest.py verify
    python scripts/data_manifest.py verify --data-dir data/processed

    # 看指纹摘要（不读数据，秒出）
    python scripts/data_manifest.py show

`verify` 只认**内容**：行按主键排序、浮点按原始位模式、NaN 先抹平后取指纹，
所以换 pyarrow/pandas 版本、文件字节变了都判通过；真正的数据差异
（少几天、复权因子变了、某列类型变了）会立刻报出来，并指出是哪几列。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a_share_quant.data.manifest import (  # noqa: E402
    DATASETS,
    DEFAULT_MANIFEST_PATH,
    describe_dataset,
    describe_manifest_entry,
    load_manifest,
    update_manifest,
    verify_dataset,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 每日增量更新的数据集：指纹会随后变化，写的时候要说清楚
MOVING_WINDOW = {"processed_2025_2026"}


def _registry() -> dict[str, dict[str, str]]:
    return {d["key"]: dict(d) for d in DATASETS}


def _guess_key(data_dir: Path) -> str:
    """从目录名反推数据集 key（--data-dir 没配 --key 时用）。"""
    name = data_dir.resolve().name
    return name if name in _registry() else name


def _rel(path: Path) -> str:
    """项目内路径记相对路径（换机器也能对得上），否则记绝对路径。"""
    p = Path(path).resolve()
    try:
        return p.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _match_by_path(datasets: dict[str, dict], data_dir: Path) -> str | None:
    """目录名猜不到 key 时，用条目里记的 data_dir 反查。

    `write --data-dir D --key K` 允许 key 与目录名不同，这种情况下 verify
    只靠目录名会找不到条目，所以写入时必须把路径一起记下来。
    """
    want = Path(data_dir).resolve()
    for key, entry in datasets.items():
        got = entry.get("data_dir")
        if not got:
            continue
        p = Path(got)
        p = (PROJECT_ROOT / p) if not p.is_absolute() else p
        if p.resolve() == want:
            return key
    return None


def cmd_write(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    targets: list[tuple[str, Path, dict[str, str]]] = []

    if args.data_dir:
        d = Path(args.data_dir)
        key = args.key or _guess_key(d)
        targets.append((key, d, _registry().get(key, {})))
    else:
        for key, spec in _registry().items():
            d = PROJECT_ROOT / spec["dir"]
            if (d / "bars.parquet").exists():
                targets.append((key, d, spec))
            else:
                print(f"[skip] {key}: {spec['dir']} 不存在（本机没有这份数据）")

    if not targets:
        print("没有可写的目标：用 --data-dir 指定目录")
        return 1

    for key, data_dir, spec in targets:
        print(f"[write] {key} <- {data_dir}")
        entry = describe_dataset(
            data_dir,
            data_dir=_rel(data_dir),
            label=args.label or spec.get("label"),
            build_command=spec.get("build_command"),
            note=(
                "每日增量更新，指纹随数据增长而变；校验前先确认更新到哪一天"
                if key in MOVING_WINDOW else None
            ),
        )
        entry["written_at"] = datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds")
        update_manifest(manifest_path, key, entry)
        print(f"        {describe_manifest_entry(entry)}")

    print(f"\n[done] {manifest_path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    payload = load_manifest(manifest_path)
    registry = _registry()

    if args.data_dir:
        d = Path(args.data_dir)
        datasets = payload.get("datasets", {})
        key = args.key or _guess_key(d)
        if key not in datasets:
            key = _match_by_path(datasets, d) or key
        targets = [(key, d)]
    else:
        targets = []
        for key, spec in payload.get("datasets", {}).items():
            d = PROJECT_ROOT / registry.get(key, {}).get("dir", f"data/{key}")
            targets.append((key, d))

    failed = 0
    skipped = 0
    for key, data_dir in targets:
        entry = payload.get("datasets", {}).get(key)
        if entry is None:
            known = ", ".join(sorted(payload.get("datasets", {}))) or "（空）"
            print(f"[fail] {key}: manifest 里没有这一项；已有：{known}")
            failed += 1
            continue
        if not (data_dir / "bars.parquet").exists():
            print(f"[skip] {key}: {data_dir} 在本机不存在（未重建数据，跳过）")
            skipped += 1
            continue

        problems = verify_dataset(data_dir, entry)
        fatal = [p for p in problems if not p.startswith("[info]")]
        if fatal:
            failed += 1
            print(f"[fail] {key}  ({data_dir})")
            for p in problems:
                print(f"       - {p}")
            print("       → 本地数据与 manifest 不是同一份。若是自己更新过数据，"
                  "属正常；若要复现报告里的数字，需要完全一致的那一份。")
        else:
            print(f"[ ok ] {key}  ({data_dir})")
            for p in problems:
                print(f"       {p}")

    print()
    if failed:
        print(f"校验失败：{failed} 份不一致" + (f"，{skipped} 份本机缺失" if skipped else ""))
        return 1
    print("校验通过" + (f"（{skipped} 份本机缺失，已跳过）" if skipped else ""))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    payload = load_manifest(Path(args.manifest))
    datasets = payload.get("datasets", {})
    if not datasets:
        print("manifest 是空的")
        return 1
    print(f"{'数据集':22s} {'范围':28s} {'标的':>6s} {'行数':>11s}  {'指纹':12s}  写入时间")
    for key in sorted(datasets):
        e = datasets[key]
        s = e.get("summary", {})
        window = f"{s.get('first_date', '?')}~{s.get('last_date', '?')}"
        print(f"{key:22s} {window:28s} {s.get('n_symbols') or 0:>6} "
              f"{s.get('bars_rows', 0):>11,}  "
              f"{str(e.get('dataset_sha256'))[:12]:12s}  {e.get('written_at', '?')}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(PROJECT_ROOT / DEFAULT_MANIFEST_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("write", cmd_write), ("verify", cmd_verify), ("show", cmd_show)):
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
        if name != "show":
            p.add_argument("--data-dir", default="",
                           help="只处理这个目录；不填则处理 manifest 里的全部")
            p.add_argument("--key", default="", help="数据集名（默认取目录名）")
        if name == "write":
            p.add_argument("--label", default="", help="人读的说明")

    args = ap.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
