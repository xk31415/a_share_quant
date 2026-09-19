"""Point-in-time 查询工具：任何 as_of 时刻只能看到当时已公布的信息。"""
from __future__ import annotations

import pandas as pd


def latest_known(df: pd.DataFrame, as_of,
                 date_col: str = "trade_date",
                 group_col: str = "symbol") -> pd.DataFrame:
    """对每个 group 取 date_col <= as_of 的最后一行（无则该组缺席）。

    用途：
      * 状态表的时间点查询（date_col=trade_date）；
      * 财务数据的时间点查询（date_col 必须是 announce_date 公告日），
        从机制上防止使用尚未公布的未来财务数据。
    """
    if df is None or df.empty:
        return df.iloc[0:0].copy() if df is not None else pd.DataFrame()
    ts = pd.Timestamp(as_of).normalize()
    past = df.loc[pd.to_datetime(df[date_col]) <= ts]
    if past.empty:
        return past
    return (
        past.sort_values([group_col, date_col])
        .groupby(group_col, as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )
