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
    coerce_bool,
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

    def __init__(self, root: str | Path, use_cache: bool = True) -> None:
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"data root not found: {self.root}")
        # 缓存已解析的表：一次回测里 universe 会按每次调仓反复取
        # instruments/status/calendar，不缓存就会把整表重读 30+ 次。
        # provider 视为只读，缓存安全；运行中替换数据文件需新建 provider。
        self.use_cache = use_cache
        self._cache: dict[str, pd.DataFrame] = {}
        self._calendar: TradingCalendar | None = None

    # -- 内部 --
    def _read_table(self, candidates: tuple[str, ...], name: str) -> pd.DataFrame:
        if self.use_cache and name in self._cache:
            return self._cache[name]
        for fname in candidates:
            path = self.root / fname
            if path.exists():
                if path.suffix == ".parquet":
                    df = pd.read_parquet(path)
                    # 前导零一旦以数值类型写进 parquet 就无法还原（000001 -> 1）。
                    # 这里显式拒绝，而不是让代码变成 "1" 后在白名单里静默消失。
                    if "symbol" in df.columns and pd.api.types.is_numeric_dtype(df["symbol"]):
                        raise ValueError(
                            f"{path.name} 的 symbol 列是数值类型 "
                            f"({df['symbol'].dtype})，A 股代码的前导零已丢失"
                            f"（000001 会变成 1）。请把 symbol 写成字符串后重新导出。")
                else:
                    # symbol 必须按字符串读入：A 股代码有前导零（000001、000300），
                    # 交给 pandas 推断类型会把 000001 读成 1，与 instruments 对不上，
                    # 板块前缀匹配也会全部失效。
                    df = pd.read_csv(path, dtype={"symbol": str})
                if self.use_cache:
                    self._cache[name] = df
                return df
        raise FileNotFoundError(
            f"{name} file not found under {self.root} (looked for: {', '.join(candidates)})"
        )

    def clear_cache(self) -> None:
        """数据文件发生变化后手动失效缓存。"""
        self._cache.clear()
        self._calendar = None

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
        raw = self._read_table(self._BAR_FILES, "bars")
        missing = [c for c in BAR_REQUIRED_COLUMNS if c not in raw.columns]
        if missing:
            raise KeyError(f"bars missing required columns: {missing}")
        key = "bars:coerced"
        if self.use_cache and key in self._cache:
            coerced = self._cache[key]
        else:
            coerced = coerce_bars(raw)
            if self.use_cache:
                self._cache[key] = coerced
        return self._slice_dates(coerced, start, end, symbols)

    def get_instruments(self) -> pd.DataFrame:
        key = "instruments:typed"
        if self.use_cache and key in self._cache:
            return self._cache[key].copy()
        df = self._read_table(self._INSTRUMENT_FILES, "instruments")
        missing = [c for c in INSTRUMENT_REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise KeyError(f"instruments missing required columns: {missing}")
        out = df.copy()
        out["symbol"] = out["symbol"].astype(str)
        out["list_date"] = pd.to_datetime(out["list_date"])
        out["delist_date"] = pd.to_datetime(out["delist_date"])
        if self.use_cache:
            self._cache[key] = out
        return out.copy()

    def get_status(self, start, end, symbols=None) -> pd.DataFrame:
        key = "status:typed"
        if self.use_cache and key in self._cache:
            out = self._cache[key]
        else:
            df = self._read_table(self._STATUS_FILES, "status")
            missing = [c for c in STATUS_REQUIRED_COLUMNS if c not in df.columns]
            if missing:
                raise KeyError(f"status missing required columns: {missing}")
            out = df.copy()
            out["trade_date"] = pd.to_datetime(out["trade_date"])
            out["symbol"] = out["symbol"].astype(str)
            out["is_st"] = coerce_bool(out["is_st"])
            out["is_suspended"] = coerce_bool(out["is_suspended"])
            if self.use_cache:
                self._cache[key] = out
        return self._slice_dates(out, start, end, symbols)

    def get_benchmark(self, code: str):
        """读取基准指数序列（benchmark.csv: trade_date, symbol, close）。

        基准单独成文件而不是塞进 bars：它没有证券基本信息、不该参与选股，
        混进 bars 会被校验器判成"缺 instruments 记录"。
        """
        path = self.root / "benchmark.csv"
        if not path.exists():
            return None
        df = pd.read_csv(path, dtype={"symbol": str})
        sub = df[df["symbol"] == str(code)]
        if sub.empty:
            return None
        idx = pd.to_datetime(sub["trade_date"]).dt.normalize()
        series = pd.Series(
            pd.to_numeric(sub["close"], errors="coerce").to_numpy(), index=idx)
        series = series.sort_index().dropna()
        if series.empty:
            return None
        series.name = str(code)
        return series

    def get_calendar(self) -> TradingCalendar:
        if self._calendar is not None:
            return self._calendar
        path = self.root / "calendar.csv"
        if not path.exists():
            raise FileNotFoundError(f"calendar.csv not found under {self.root}")
        self._calendar = TradingCalendar(pd.to_datetime(pd.read_csv(path)["trade_date"]))
        return self._calendar

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
