"""增量更新的纯逻辑：表合并与一致性检查。

放在包里而不是脚本里，是为了能离线单测——这几条规则一旦出错，
表现是「数据看着还在、但某几天的行情被静默删掉了」，非常难发现。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

KEY = ["trade_date", "symbol"]


def upsert(old: pd.DataFrame, new: pd.DataFrame,
           key: list[str] | None = None) -> pd.DataFrame:
    """按 ``key`` 用 new 覆盖 old，其余行原样保留。

    不能写成 ``old[old.trade_date < new.min()] + new``：那样会把
    「窗口内没有行情的停牌股」的旧记录一并删掉，等于用一次更新制造数据缺口。
    只删掉 (trade_date, symbol) 真正出现在 new 里的那些行。
    """
    key = list(key or KEY)
    missing = [k for k in key if k not in old.columns or k not in new.columns]
    if missing:
        raise ValueError(f"缺少合并键：{missing}")
    if new.empty:
        return old.copy()

    new_keys = pd.MultiIndex.from_frame(new[key])
    old_keys = pd.MultiIndex.from_frame(old[key])
    keep_old = old[~old_keys.isin(new_keys)]
    out = pd.concat([keep_old, new], ignore_index=True)
    out = out.drop_duplicates(subset=key, keep="last")
    return out.sort_values(key).reset_index(drop=True)


def check_continuity(bars: pd.DataFrame, min_ratio: float = 0.5,
                     date_col: str = "trade_date",
                     symbol_col: str = "symbol") -> list[str]:
    """每日证券数不能突然腰斩——那是拉取中断的典型症状。

    返回问题描述列表（空列表表示通过）。阈值 0.5 是刻意宽松的：
    正常的退市/新增每天只有个位数变动，只剩一半必属异常。
    """
    if bars.empty:
        return []
    counts = bars.groupby(date_col)[symbol_col].nunique().sort_index()
    if len(counts) < 2:
        return []
    prev = counts.shift(1)
    bad = counts[(prev.notna()) & (counts < prev * min_ratio)]
    return [f"{pd.Timestamp(ts).date()} 只有 {int(n)} 只"
            f"（前一日 {int(prev.loc[ts])} 只）"
            for ts, n in bad.items()]


def atomic_write(df: pd.DataFrame, path: str | Path) -> Path:
    """写临时文件再原子替换，避免中途失败留下半张表。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    if p.suffix == ".parquet":
        df.to_parquet(tmp, index=False)
    else:
        df.to_csv(tmp, index=False)
    os.replace(tmp, p)
    return p
