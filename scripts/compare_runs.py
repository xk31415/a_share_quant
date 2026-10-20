"""并排对比多个实验结果（样本内 / 样本外 / 不同策略）。

用法：
    python scripts/compare_runs.py results/EXP-xxx results/EXP-yyy ...
    python scripts/compare_runs.py --last 6
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

COLS = [
    ("strategy", "策略", 12),
    ("label", "配置", 26),
    ("annualized_return", "年化", 8),
    ("annualized_volatility", "波动", 8),
    ("sharpe_ratio", "Sharpe", 7),
    ("maximum_drawdown", "回撤", 8),
    ("excess_annualized_return", "超额", 8),
    ("information_ratio", "IR", 6),
    ("beta", "beta", 6),
    ("total_turnover", "换手", 6),
    ("annualized_cost_drag", "成本", 7),
]


def load(exp_dir: Path) -> dict | None:
    mpath = exp_dir / "metrics.json"
    if not mpath.exists():
        return None
    metrics = json.loads(mpath.read_text(encoding="utf-8"))
    rec = ROOT / "experiments" / f"{exp_dir.name}.json"
    params = {}
    if rec.exists():
        params = json.loads(rec.read_text(encoding="utf-8")).get("parameters") or {}
    row = dict(metrics)
    row["strategy"] = params.get("strategy") or "?"
    bits = []
    if params.get("seed"):
        bits.append(f"seed={params['seed']}")
    if params.get("liquidity_pct"):
        bits.append(f"流动≤{params['liquidity_pct']:.0%}")
    if params.get("industry_neutral"):
        bits.append(f"行业中性k={params.get('k_per_industry')}")
    if params.get("window"):
        bits.append(f"w={params['window']}")
    if params.get("skip") is not None:
        bits.append(f"skip={params['skip']}")
    cap = params.get("initial_capital")
    if cap and cap != 1_000_000:
        bits.append(f"本金{cap/1e8:.1f}亿")
    if params.get("capacity_aum"):
        bits.append("容量约束")
    row["label"] = ",".join(bits) or "-"
    row["exp"] = exp_dir.name
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*")
    ap.add_argument("--last", type=int, default=0, help="取最近 N 个实验")
    args = ap.parse_args()

    paths = [Path(d) for d in args.dirs] if args.dirs else []
    if args.last:
        paths = [Path(p) for p in sorted(glob.glob(str(ROOT / "results" / "EXP-*")),
                                        key=os.path.getmtime)[-args.last:]]
    if not paths:
        raise SystemExit("给出实验目录，或用 --last N")

    rows = [r for r in (load(p) for p in paths) if r]
    if not rows:
        raise SystemExit("没有可读的实验")

    header = "  ".join(f"{name:>{w}s}" for _, name, w in COLS)
    print(header)
    print("-" * len(header))
    for r in rows:
        cells = []
        for key, _, w in COLS:
            v = r.get(key)
            if key in ("strategy", "label"):
                cells.append(f"{str(v)[:w]:>{w}s}")
            elif isinstance(v, (int, float)):
                cells.append(f"{v:>{w}.3f}")
            else:
                cells.append(f"{'-':>{w}s}")
        print("  ".join(cells))
        print(f"{'':>14s}→ {r['exp']}")


if __name__ == "__main__":
    main()
