"""统一数据 schema：日线行情、证券基本信息、每日状态、财务数据。

核心约定（防止数据泄露）：
  * 复权价（adj_close，后复权）只用于信号/因子计算；
  * 原始价（open/high/low/close/limit_up/limit_down/pre_close）只用于成交模拟
    与涨跌停约束；
  * 财务数据必须带公告日 announce_date，t 日信号只能使用 announce_date <= t 的行。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# ---- 日线行情（long format：一行 = 一个 (trade_date, symbol)）----
BAR_REQUIRED_COLUMNS: dict[str, str] = {
    "trade_date": "datetime64[ns]",
    "symbol": "object",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "amount": "float64",
}

BAR_OPTIONAL_COLUMNS: tuple[str, ...] = (
    "adj_close",           # 后复权收盘价：信号/因子 + 估值还原（k = adj_close/close）
    "pre_close",           # 昨收（原始价）
    "limit_up",            # 涨停价（原始价）
    "limit_down",          # 跌停价（原始价）
    "limit_exempt",        # 当日无涨跌幅限制（如注册制新股上市前 5 日）
    "is_suspended",        # 当日是否停牌
    "is_st",               # 当日是否 ST
    "industry",            # 行业分类（历史快照）
    "market_cap",          # 总市值（历史快照）
    "shares_outstanding",  # 总股本
)

# ---- 证券基本信息 ----
INSTRUMENT_REQUIRED_COLUMNS: dict[str, str] = {
    "symbol": "object",
    "name": "object",
    "list_date": "datetime64[ns]",
    "delist_date": "datetime64[ns]",   # 未退市为 NaT
}

# ---- 每日状态（逐交易日覆盖，可按 as_of 查询）----
STATUS_REQUIRED_COLUMNS: dict[str, str] = {
    "trade_date": "datetime64[ns]",
    "symbol": "object",
    "is_st": "bool",
    "is_suspended": "bool",
}

# ---- 财务数据（扩展用）----
FUNDAMENTALS_REQUIRED_COLUMNS: dict[str, str] = {
    "symbol": "object",
    "announce_date": "datetime64[ns]",  # 公告日：t 日信号只能用 announce_date <= t 的行
    "report_date": "datetime64[ns]",    # 报告期
}


@dataclass(frozen=True)
class PriceUsagePolicy:
    """复权价 / 原始价用途的**声明性说明**（见 README「数据模型」一节）。

    注意：这是一个常量，**不参与运行时强制**。真正的保证来自约定——
    因子只读 ``adj_close``；成交、涨跌停、整手只用原始价；
    估值用 ``adj_close/close`` 得到的复权因子还原创除权跳空（见
    ``PortfolioState.position_value``）。新增代码请遵守本声明，
    不要把它当成运行时会自动拦截的检查器。
    """

    signal_columns: tuple[str, ...] = ("adj_close",)          # 因子/信号默认输入（后复权）
    execution_columns: tuple[str, ...] = ("open", "high", "low", "close")  # 成交模拟（原始）
    constraint_columns: tuple[str, ...] = ("limit_up", "limit_down", "pre_close")  # 约束（原始）
    must_be_raw: tuple[str, ...] = ("open", "high", "low", "close", "volume",
                                    "amount", "limit_up", "limit_down", "pre_close")
    may_be_adjusted: tuple[str, ...] = ("adj_close",)


PRICE_USAGE_POLICY = PriceUsagePolicy()

_BOOL_TRUE = {"true", "1", "t", "yes", "y", "是"}
_BOOL_FALSE = {"false", "0", "f", "no", "n", "", "否"}


def coerce_bool(series: pd.Series) -> pd.Series:
    """把布尔列转换为真正的 bool dtype。

    不能直接用 ``astype(bool)``：对 object 列里的字符串 "False"，
    ``bool("False") == True``——会把"未停牌"静默变成"停牌"（或反之）。
    这里显式按字面值映射，无法识别的值按 False 处理。
    """
    if pd.api.types.is_bool_dtype(series):
        return series.astype(bool)
    if pd.api.types.is_numeric_dtype(series):
        return series.fillna(0).astype(bool)
    text = series.astype("string").str.strip().str.lower()
    mapped = text.map(lambda v: True if v in _BOOL_TRUE
                      else (False if v in _BOOL_FALSE else pd.NA))
    return mapped.fillna(False).astype(bool)


def coerce_bars(df: pd.DataFrame) -> pd.DataFrame:
    """把行情表转换为规范 dtype，并按 (symbol, trade_date) 排序。"""
    out = df.copy()
    for col, dtype in BAR_REQUIRED_COLUMNS.items():
        if col not in out.columns:
            raise KeyError(f"bars missing required column: {col!r}")
        if dtype == "datetime64[ns]":
            out[col] = pd.to_datetime(out[col])
        elif dtype == "object":
            out[col] = out[col].astype(str)
        else:
            out[col] = pd.to_numeric(out[col])
    for col in BAR_OPTIONAL_COLUMNS:
        if col not in out.columns:
            continue
        if col.startswith("is_") or col == "limit_exempt":
            out[col] = coerce_bool(out[col])
        elif col not in ("industry",):
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
