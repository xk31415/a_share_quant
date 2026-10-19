"""从掘金（gm）或 QMT（xtquant）拉取真实行情，装配成引擎可读的数据集。

用法：
    # 掘金（推荐：一次拿齐 is_st / is_suspended / 涨跌停价）
    set GM_TOKEN=xxxx
    python scripts/build_dataset.py --source gm --start 2020-01-01 --end 2024-12-31

    # 先小样本验证链路
    python scripts/build_dataset.py --source gm --start 2023-01-01 --end 2023-12-31 \
        --limit-symbols 600000,000001,300001 --min-symbols-per-day 1

    # 只看 SDK 返回了什么字段（排查字段名漂移）
    python scripts/build_dataset.py --source gm --probe

    # QMT（降级源：无历史 ST / 无历史涨跌停价）
    python scripts/build_dataset.py --source qmt --allow-missing-limits

产出（--out，默认 data/processed）：
    bars.parquet  instruments.parquet  status.parquet  calendar.csv

!!! 本脚本只负责取数与落地，不做任何策略判断。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a_share_quant.data.builders import (  # noqa: E402
    DEFAULT_BOARDS,
    BuildReport,
    assemble,
    self_check,
    split_symbol,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 掘金
class GmSource:
    """掘金量化 gm SDK。token 走环境变量 GM_TOKEN。"""

    name = "gm"

    def __init__(self) -> None:
        token = os.environ.get("GM_TOKEN", "").strip()
        if not token:
            raise SystemExit("缺少环境变量 GM_TOKEN（掘金 token）")
        from gm.api import set_token
        set_token(token)

    # -- 探针 --
    def probe(self) -> None:
        from gm.api import get_trading_dates, get_instruments, get_history_instruments, history
        cal = get_trading_dates(exchange="SHSE", start_date="2024-01-02", end_date="2024-01-05")
        print("trading_dates:", cal[:3])
        inst = get_instruments(exchanges=["SHSE"], sec_types=[1], df=True)
        print("instruments columns:", list(inst.columns), "rows:", len(inst))
        print(inst.head(2).to_string())
        bar = history(symbol="SHSE.600000", frequency="1d", start_time="2024-01-02",
                      end_time="2024-01-05", df=True)
        print("history columns:", list(bar.columns))
        print(bar.head(2).to_string())
        hi = get_history_instruments(symbols=["SHSE.600000"], start_date="2024-01-02",
                                     end_date="2024-01-05", df=True)
        print("history_instruments columns:", list(hi.columns))
        print(hi.head(2).to_string())

    # -- 取数 --
    def calendar(self, start: str, end: str) -> list[str]:
        from gm.api import get_trading_dates
        return list(get_trading_dates(exchange="SHSE", start_date=start, end_date=end))

    def instruments(self) -> pd.DataFrame:
        from gm.api import get_instruments
        df = get_instruments(exchanges=["SHSE", "SZSE", "BJSE"], sec_types=[1],
                             fields="symbol,sec_name,listed_date,delisted_date", df=True)
        syms = [split_symbol(s) for s in df["symbol"]]
        out = pd.DataFrame({
            "symbol": [s for s, _ in syms],
            "exchange": [e for _, e in syms],
            "name": df.get("sec_name", pd.Series([""] * len(df))).astype(str),
            "list_date": pd.to_datetime(df["listed_date"], errors="coerce"),
            "delist_date": pd.to_datetime(df["delisted_date"], errors="coerce"),
        })
        return out

    def bars(self, symbols: list[str], start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """返回 (原始价, 后复权收盘)。"""
        from gm.api import ADJUST_NONE, ADJUST_POST, history
        raw_parts, post_parts = [], []
        for i in range(0, len(symbols), 100):
            batch = symbols[i:i + 100]
            gm_syms = [f"{split_symbol(s)[1]}.{split_symbol(s)[0]}" for s in batch]
            for adjust, bucket in ((ADJUST_NONE, raw_parts), (ADJUST_POST, post_parts)):
                df = history(symbol=gm_syms, frequency="1d", start_time=start, end_time=end,
                             fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
                             adjust=adjust, adjust_end_time=end, df=True)
                if df is None or len(df) == 0:
                    continue
                bucket.append(_normalize_gm_bars(df, post_only=(adjust == ADJUST_POST)))
            print(f"  bars {min(i + 100, len(symbols))}/{len(symbols)}", flush=True)
        raw = pd.concat(raw_parts, ignore_index=True) if raw_parts else pd.DataFrame()
        post = pd.concat(post_parts, ignore_index=True) if post_parts else pd.DataFrame()
        return raw, post

    def status(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        from gm.api import get_history_instruments
        parts = []
        for i in range(0, len(symbols), 100):
            batch = symbols[i:i + 100]
            gm_syms = [f"{split_symbol(s)[1]}.{split_symbol(s)[0]}" for s in batch]
            df = get_history_instruments(symbols=gm_syms, start_date=start, end_date=end,
                                         df=True)
            if df is not None and len(df):
                parts.append(df)
            print(f"  status {min(i + 100, len(symbols))}/{len(symbols)}", flush=True)
        if not parts:
            return pd.DataFrame()
        df = pd.concat(parts, ignore_index=True)
        syms = [split_symbol(s) for s in df["symbol"]]
        out = pd.DataFrame({
            "trade_date": pd.to_datetime(df["trade_date"]).dt.normalize(),
            "symbol": [s for s, _ in syms],
            "is_st": df.get("is_st", False),
            "is_suspended": df.get("is_suspended", False),
            "limit_up": pd.to_numeric(df.get("upper_limit"), errors="coerce"),
            "limit_down": pd.to_numeric(df.get("lower_limit"), errors="coerce"),
        })
        out["is_st"] = out["is_st"].fillna(False).astype(bool)
        out["is_suspended"] = out["is_suspended"].fillna(False).astype(bool)
        return out


def _normalize_gm_bars(df: pd.DataFrame, post_only: bool) -> pd.DataFrame:
    syms = [split_symbol(s) for s in df["symbol"]]
    date_col = "eob" if "eob" in df.columns else "trade_date"
    out = pd.DataFrame({
        "trade_date": pd.to_datetime(df[date_col]).dt.normalize(),
        "symbol": [s for s, _ in syms],
    })
    if post_only:
        out["adj_close"] = pd.to_numeric(df["close"], errors="coerce")
        return out
    for col in ("open", "high", "low", "close", "volume", "amount", "pre_close"):
        src = col if col in df.columns else ("preclose" if col == "pre_close" else None)
        out[col] = pd.to_numeric(df[src], errors="coerce") if src else float("nan")
    return out


# ---------------------------------------------------------------- QMT
class QmtSource:
    """迅投 QMT / MiniQMT（xtquant）。

    降级源：日线不提供历史 ST 与历史涨跌停价。
      * is_st        —— 只能用**当前**证券名称前缀回溯，存在前视偏差；
      * limit_up/down —— 只能留空（需要 --allow-missing-limits 并在配置里关掉涨跌停约束）。
    """

    name = "qmt"

    def __init__(self) -> None:
        from xtquant import xtdata
        self.xt = xtdata

    def probe(self) -> None:
        codes = self.xt.get_stock_list_in_sector("沪深A股")[:3]
        print("stock_list:", codes)
        print("instrument_detail:", self.xt.get_instrument_detail(codes[0]))
        self.xt.download_history_data(codes[0], period="1d", start_time="20240102",
                                      end_time="20240105")
        data = self.xt.get_market_data_ex(field_list=[], stock_list=[codes[0]],
                                          period="1d", start_time="20240102",
                                          end_time="20240105", dividend_type="none")
        frame = data.get(codes[0])
        print("bar columns:", list(frame.columns) if frame is not None else None)
        print(frame.head(2).to_string() if frame is not None else "空")

    def calendar(self, start: str, end: str) -> list[str]:
        stamps = self.xt.get_trading_dates("SH", start_time=start.replace("-", ""),
                                           end_time=end.replace("-", ""))
        return [pd.Timestamp(int(t), unit="ms").strftime("%Y-%m-%d") for t in stamps]

    def instruments(self) -> pd.DataFrame:
        codes = self.xt.get_stock_list_in_sector("沪深A股")
        rows = []
        for code in codes:
            detail = self.xt.get_instrument_detail(code) or {}
            sym, ex = split_symbol(code)
            rows.append({
                "symbol": sym, "exchange": ex,
                "name": str(detail.get("InstrumentName", "")),
                "list_date": pd.to_datetime(str(detail.get("OpenDate", "")), errors="coerce"),
                "delist_date": pd.to_datetime(str(detail.get("ExpireDate", "")), errors="coerce"),
            })
        return pd.DataFrame(rows)

    def bars(self, symbols: list[str], start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        codes = [f"{split_symbol(s)[0]}.{split_symbol(s)[1]}" if split_symbol(s)[1]
                 else s for s in symbols]
        for code in codes:
            self.xt.download_history_data(code, period="1d",
                                          start_time=start.replace("-", ""),
                                          end_time=end.replace("-", ""))
        fields = ["open", "high", "low", "close", "volume", "amount", "preClose"]
        raw = self._market(codes, fields, start, end, "none")
        post = self._market(codes, ["close"], start, end, "back")
        return raw, post

    def _market(self, codes, fields, start, end, dividend) -> pd.DataFrame:
        data = self.xt.get_market_data_ex(field_list=fields, stock_list=codes, period="1d",
                                          start_time=start.replace("-", ""),
                                          end_time=end.replace("-", ""),
                                          dividend_type=dividend, fill_data=False)
        rows = []
        for code, frame in (data or {}).items():
            if frame is None or len(frame) == 0:
                continue
            sym, _ = split_symbol(code)
            frame = frame.reset_index().rename(columns={"index": "trade_date"})
            frame["symbol"] = sym
            if "preClose" in frame.columns:
                frame = frame.rename(columns={"preClose": "pre_close"})
            rows.append(frame)
        if not rows:
            return pd.DataFrame()
        out = pd.concat(rows, ignore_index=True)
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
        if dividend == "back":
            return out[["trade_date", "symbol", "close"]].rename(
                columns={"close": "adj_close"})
        return out

    def status(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        codes = [f"{split_symbol(s)[0]}.{split_symbol(s)[1]}" for s in symbols]
        fields = ["suspendFlag"]
        df = self._market(codes, fields, start, end, "none")
        if df.empty:
            return df
        out = df[["trade_date", "symbol"]].copy()
        out["is_suspended"] = df.get("suspendFlag", 0).fillna(0).astype(bool)
        out["is_st"] = False          # QMT 无历史 ST，只能事后用当前名称回溯
        out["limit_up"] = float("nan")
        out["limit_down"] = float("nan")
        return out


# ---------------------------------------------------------------- baostock
class BaostockSource:
    """baostock（免费）。有逐日 isST / tradestatus / 后复权，无历史涨跌停价。

    涨跌停由 ``builders.derive_limits`` 按交易所规则推算，并用"收盘价不得越过
    涨跌停"反查规则表是否写错。
    """

    name = "baostock"
    RAW_FIELDS = ("date,open,high,low,close,preclose,volume,amount,"
                  "tradestatus,isST,pctChg")

    def __init__(self) -> None:
        import baostock as bs
        self.bs = bs
        lg = bs.login()
        if str(lg.error_code) != "0":
            raise SystemExit(f"baostock 登录失败：{lg.error_msg}")
        self._ex: dict[str, str] = {}

    def close(self) -> None:
        try:
            self.bs.logout()
        except Exception:
            pass

    # -- 探针 --
    def probe(self) -> None:
        rs = self.bs.query_stock_basic()
        print("stock_basic fields:", rs.fields)
        rs = self.bs.query_history_k_data_plus(
            "sh.600000", self.RAW_FIELDS, start_date="2024-01-02",
            end_date="2024-01-05", frequency="d", adjustflag="3")
        print("bars fields:", rs.fields)
        while rs.next():
            print(" ", rs.get_row_data())
        rs = self.bs.query_trade_dates(start_date="2024-01-02", end_date="2024-01-05")
        print("trade_dates fields:", rs.fields)

    # -- 取数 --
    def calendar(self, start: str, end: str) -> list[str]:
        rs = self.bs.query_trade_dates(start_date=start, end_date=end)
        if str(rs.error_code) != "0":
            raise SystemExit(f"交易日历查询失败：{rs.error_msg}")
        out = []
        while rs.next():
            row = dict(zip(rs.fields, rs.get_row_data()))
            if row["is_trading_day"] == "1":
                out.append(row["calendar_date"])
        return out

    def instruments(self) -> pd.DataFrame:
        rs = self.bs.query_stock_basic()
        rows = []
        while rs.next():
            row = dict(zip(rs.fields, rs.get_row_data()))
            if row["type"] != "1":          # 1=股票；2=指数，4/5=其它
                continue
            if not row["code"].startswith(("sh.", "sz.")):
                continue
            sym, ex = split_symbol(row["code"])
            self._ex[sym] = ex
            rows.append({
                "symbol": sym, "exchange": ex, "name": row["code_name"],
                "list_date": pd.to_datetime(row["ipoDate"], errors="coerce"),
                "delist_date": pd.to_datetime(row["outDate"], errors="coerce"),
            })
        return pd.DataFrame(rows)

    def fetch_all(self, symbols: list[str], start: str, end: str):
        raw_parts, post_parts, status_parts = [], [], []
        total = len(symbols)
        for n, sym in enumerate(symbols, 1):
            code = f"{'sh' if self._ex.get(sym) == 'SHSE' else 'sz'}.{sym}"
            rs = self.bs.query_history_k_data_plus(
                code, self.RAW_FIELDS, start_date=start, end_date=end,
                frequency="d", adjustflag="3")
            if str(rs.error_code) != "0":
                print(f"[warn] {code} 行情查询失败：{rs.error_msg}")
                continue
            rows = []
            while rs.next():
                rows.append(dict(zip(rs.fields, rs.get_row_data())))
            if not rows:
                continue
            df = pd.DataFrame(rows)
            df["trade_date"] = pd.to_datetime(df["date"])
            df["symbol"] = sym
            for col in ("open", "high", "low", "close", "preclose", "volume", "amount"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            st = pd.DataFrame({
                "trade_date": df["trade_date"], "symbol": sym,
                "is_st": df["isST"] == "1",
                "is_suspended": df["tradestatus"] == "0",
                "limit_up": float("nan"), "limit_down": float("nan"),
            })
            status_parts.append(st)
            priced = df[df["close"].notna()][
                ["trade_date", "symbol", "open", "high", "low", "close",
                 "volume", "amount", "preclose"]].rename(
                columns={"preclose": "pre_close"})
            raw_parts.append(priced)

            rs2 = self.bs.query_history_k_data_plus(
                code, "date,close", start_date=start, end_date=end,
                frequency="d", adjustflag="1")
            rows2 = []
            while rs2.next():
                rows2.append(dict(zip(rs2.fields, rs2.get_row_data())))
            if rows2:
                d2 = pd.DataFrame(rows2)
                d2["trade_date"] = pd.to_datetime(d2["date"])
                d2["symbol"] = sym
                d2["adj_close"] = pd.to_numeric(d2["close"], errors="coerce")
                post_parts.append(d2[["trade_date", "symbol", "adj_close"]])

            if n % 50 == 0 or n == total:
                print(f"  {n}/{total} 只", flush=True)

        cat = lambda parts, cols=None: (pd.concat(parts, ignore_index=True)  # noqa: E731
                                        if parts else pd.DataFrame())
        return cat(raw_parts), cat(post_parts), cat(status_parts)


# ---------------------------------------------------------------- 落地
def write_tables(report: BuildReport, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    try:
        report.bars.to_parquet(out / "bars.parquet", index=False)
        report.instruments.to_parquet(out / "instruments.parquet", index=False)
        report.status.to_parquet(out / "status.parquet", index=False)
    except (ImportError, ValueError):
        print("[warn] 未安装 pyarrow，退回 CSV（注意 symbol 前导零）")
        report.bars.to_csv(out / "bars.csv", index=False)
        report.instruments.to_csv(out / "instruments.csv", index=False)
        report.status.to_csv(out / "status.csv", index=False)
    report.calendar.to_csv(out / "calendar.csv", index=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["baostock", "gm", "qmt"], default="baostock")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "data" / "processed"))
    ap.add_argument("--boards", default=",".join(DEFAULT_BOARDS),
                    help="main / chinext / star / bse，逗号分隔")
    ap.add_argument("--limit-symbols", default="",
                    help="只处理这些 6 位代码，逗号分隔（小样本调试）")
    ap.add_argument("--allow-missing-limits", action="store_true",
                    help="允许涨跌停价缺失（对应配置里关闭 consider_limit_up_down）")
    ap.add_argument("--min-symbols-per-day", type=int, default=100)
    ap.add_argument("--probe", action="store_true", help="只打印 SDK 返回的字段")
    args = ap.parse_args()

    source = {"baostock": BaostockSource, "gm": GmSource, "qmt": QmtSource}[args.source]()
    if args.probe:
        source.probe()
        return

    print(f"[1/4] 交易日历 {args.start} ~ {args.end}")
    calendar = source.calendar(args.start, args.end)
    print(f"      {len(calendar)} 个交易日")

    print("[2/4] 证券列表")
    instruments = source.instruments()
    print(f"      {len(instruments)} 只，已退市 {int(instruments['delist_date'].notna().sum())} 只")

    boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    from a_share_quant.data.builders import select_boards
    picked = select_boards(instruments, boards)
    symbols = picked["symbol"].tolist()
    if args.limit_symbols:
        wanted = {s.strip() for s in args.limit_symbols.split(",") if s.strip()}
        symbols = [s for s in symbols if s in wanted]
    print(f"      白名单 {boards} 内 {len(symbols)} 只")

    print("[3/4] 行情与状态")
    if hasattr(source, "fetch_all"):
        bars_raw, bars_post, status = source.fetch_all(symbols, args.start, args.end)
    else:
        bars_raw, bars_post = source.bars(symbols, args.start, args.end)
        status = source.status(symbols, args.start, args.end)
    print(f"      bars {len(bars_raw)} 行 / 后复权 {len(bars_post)} 行 / 状态 {len(status)} 行")

    print("[4/4] 装配与校验")
    report = assemble(bars_raw, bars_post, status, instruments, calendar,
                      boards=boards,
                      limit_symbols=set(symbols) if args.limit_symbols else None)

    # 数据源不给涨跌停价时按规则表推算（baostock / QMT 都属此类）
    if report.bars["limit_up"].isna().all() and not args.allow_missing_limits:
        print("      源未提供涨跌停价，按交易所规则推算")
        from a_share_quant.data.builders import derive_limits
        report.bars, w = derive_limits(report.bars, report.instruments, report.calendar)
        report.warnings.extend(w)

    warnings = self_check(report, allow_missing_limits=args.allow_missing_limits,
                          min_symbols_per_day=args.min_symbols_per_day)
    write_tables(report, Path(args.out))
    if hasattr(source, "close"):
        source.close()

    for w in warnings:
        print(f"[warn] {w}")
    b = report.bars
    print(f"\n[done] {args.out}")
    print(f"  bars        {len(b):>9,} 行  {b['symbol'].nunique():>5} 只  "
          f"{b['trade_date'].min().date()} ~ {b['trade_date'].max().date()}")
    print(f"  instruments {len(report.instruments):>9,} 只  "
          f"退市 {int(report.instruments['delist_date'].notna().sum())} 只")
    print(f"  status      {len(report.status):>9,} 行")
    print(f"  calendar    {len(report.calendar):>9,} 个交易日")


if __name__ == "__main__":
    main()
