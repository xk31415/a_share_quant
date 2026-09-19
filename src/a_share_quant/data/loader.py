"""数据访问层：DataProvider 抽象接口 + 本地 CSV/Parquet 实现。"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from ..utils.dates import TradingCalendar
from .schema import (
    BAR_REQUIRED_COLUMNS,
    INSTRUMENT_REQUIRED_COLUMNS,
    STATUS_REQUIRED_COLUMNS,
    coerce_bars,
)


class DataProvider(ABC):
    """数据源统一接口。

    实现方必须遵守：
      * 返回请求区间内“当时已知”的数据，绝不填补或伪造缺失值；
      * 行情为 long format，列符合 data.schema 规范；
      * 所有 get_* 均为只读操作。
    """

    @abstractmethod
    def get_bars(self, start, end, symbols: Optional[Iterable[str]] = None) -> pd.DataFrame:
        """读取 [start, end]（闭区间）日线行情。"""

    @abstractmethod
    def get_instruments(self) -> pd.DataFrame:
        """读取证券基本信息（含上市/退市日期）。"""

    @abstractmethod
    def get_status(self, start, end, symbols: Optional[Iterable[str]] = None) -> pd.DataFrame:
        """读取 [start, end] 每日状态（ST/停牌标记）。"""

    @abstractmethod
    def get_calendar(self) -> TradingCalendar:
        """读取交易日历。"""

    def data_version(self) -> str:
        """数据版本标识（用于实验记录，保证结果可复现）。"""
        return "unversioned"


class LocalFileProvider(DataProvider):
    """从本地目录读取规范文件。

    目录结构：
      bars.parquet 或 bars.csv       —— 日线行情
      instruments.parquet 或 instruments.csv —— 证券基本信息
      status.parquet 或 status.csv   —— 每日状态
      calendar.csv                   —— 交易日历（trade_date 一列）
    """

    _BAR_FILES = ("bars.parquet", "bars.csv")
    _INSTRUMENT_FILES = ("instruments.parquet", "instruments.csv")
    _STATUS_FILES = ("status.parquet", "status.csv")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"data root not found: {self.root}")

    # -- 内部 --
    def _read_table(self, candidates: tuple[str, ...], name: str) -> pd.DataFrame:
        for fname in candidates:
            path = self.root / fname
            if path.exists():
                if path.suffix == ".parquet":
                    return pd.read_parquet(path)
                return pd.read_csv(path)
        raise FileNotFoundError(
            f"{name} file not found under {self.root} (looked for: {', '.join(candidates)})"
        )

    @staticmethod
    def _slice_dates(df: pd.DataFrame, start, end,
                     symbols: Optional[Iterable[str]]) -> pd.DataFrame:
        ts_start, ts_end = pd.Timestamp(start), pd.Timestamp(end)
        mask = (df["trade_date"] >= ts_start) & (df["trade_date"] <= ts_end)
        df = df.loc[mask]
        if symbols is not None:
            df = df.loc[df["symbol"].isin(set(symbols))]
        return df.reset_index(drop=True)

    # -- 接口实现 --
    def get_bars(self, start, end, symbols=None) -> pd.DataFrame:
        df = self._read_table(self._BAR_FILES, "bars")
        missing = [c for c in BAR_REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise KeyError(f"bars missing required columns: {missing}")
        return self._slice_dates(coerce_bars(df), start, end, symbols)

    def get_instruments(self) -> pd.DataFrame:
        df = self._read_table(self._INSTRUMENT_FILES, "instruments")
        missing = [c for c in INSTRUMENT_REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise KeyError(f"instruments missing required columns: {missing}")
        out = df.copy()
        out["list_date"] = pd.to_datetime(out["list_date"])
        out["delist_date"] = pd.to_datetime(out["delist_date"])
        return out

    def get_status(self, start, end, symbols=None) -> pd.DataFrame:
        df = self._read_table(self._STATUS_FILES, "status")
        missing = [c for c in STATUS_REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise KeyError(f"status missing required columns: {missing}")
        out = df.copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"])
        out["is_st"] = out["is_st"].astype(bool)
        out["is_suspended"] = out["is_suspended"].astype(bool)
        return self._slice_dates(out, start, end, symbols)

    def get_calendar(self) -> TradingCalendar:
        path = self.root / "calendar.csv"
        if not path.exists():
            raise FileNotFoundError(f"calendar.csv not found under {self.root}")
        return TradingCalendar(pd.to_datetime(pd.read_csv(path)["trade_date"]))

    def data_version(self) -> str:
        """以目录内文件名/大小/mtime 生成指纹，标识数据快照版本。"""
        h = hashlib.sha256()
        for f in sorted(self.root.iterdir()):
            if f.is_file():
                st = f.stat()
                h.update(f.name.encode("utf-8"))
                h.update(str(st.st_size).encode())
                h.update(str(st.st_mtime_ns).encode())
        return h.hexdigest()[:12]
