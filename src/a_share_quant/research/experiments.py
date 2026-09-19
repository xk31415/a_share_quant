"""实验记录：每次回测的可复现元数据。

目的：避免“两个月前跑过一个策略，但现在不知道当时用了什么参数”。
每次实验记录 experiment_id、策略、数据版本、区间、频率、成本、参数、
基准、结果路径与 Git commit。
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


def new_experiment_id() -> str:
    """生成 EXP-YYYYmmdd-HHMMSS-xxxx 形式的实验 ID。"""
    now = dt.datetime.now()
    return f"EXP-{now.strftime('%Y%m%d-%H%M%S')}-{now.strftime('%f')[:4]}"


def git_commit(root: Path | str | None = None) -> Optional[str]:
    """尽力获取当前 Git commit（无仓库/失败时返回 None）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root) if root else None,
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


@dataclass
class ExperimentRecord:
    """一次实验的完整元数据。"""

    experiment_id: str
    strategy_name: str
    data_version: str
    universe: str                      # 股票池规则摘要
    start_date: str
    end_date: str
    rebalance_frequency: str
    transaction_cost: Mapping[str, float]
    slippage: float
    parameters: Mapping[str, Any] = field(default_factory=dict)
    benchmark: Optional[str] = None
    result_path: Optional[str] = None
    git_commit: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    notes: str = ""


def save_experiment(record: ExperimentRecord,
                    experiments_dir: Path | str) -> Path:
    """把实验记录写入 experiments/{experiment_id}.json。"""
    path = Path(experiments_dir)
    path.mkdir(parents=True, exist_ok=True)
    out = path / f"{record.experiment_id}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(asdict(record), f, ensure_ascii=False, indent=2, default=str)
    return out


def load_experiment(path: Path | str) -> ExperimentRecord:
    """读取实验记录。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return ExperimentRecord(**raw)
