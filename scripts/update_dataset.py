"""增量更新四张表：只拉最近几天，覆盖窗口后写回。

为什么需要它：`build_dataset.py` 是全量重建（4700 只 × 5 年，20 分钟以上），
实盘每天 15:30 后跑一次不现实；而 `live_signal.py` 有 15 天数据新鲜度熔断，
不更新数据就出不了信号。所以需要一个「只补尾部几天」的轻量更新。

用法
----
    # 补最近 5 个交易日（默认重叠窗口，用于覆盖数据源的小幅修订）
    python scripts/update_dataset.py --data-dir data/processed_2025_2026

    # 只看会改什么，不写盘
    python scripts/update_dataset.py --data-dir data/processed_2025_2026 --dry-run

设计要点
--------
1. **覆盖重叠窗口而不是只追加**：数据源会修订最近几天的复权因子与涨跌停价，
   只追加会让新旧口径混在一张表里。默认回退 5 个交易日重取。
2. **按 (trade_date, symbol) 精确覆盖**，不是按日期整段截断——后者会把
   「窗口内没有行情的停牌股」的历史记录一并删掉。
3. **写盘是原子的**：先写 `.tmp` 再 `os.replace`，中途失败不会留下半张表。
4. **不信任增量结果**：合并后重新做复权因子单调性检查与每日证券数检查，
   异常直接报错而不是继续。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# scripts/ 目录在 sys.path 上，直接复用全量构建里的 Source 实现
from build_dataset import BaostockSource, GmSource, QmtSource  # noqa: E402

from a_share_quant.data.builders import (  # noqa: E402
    DEFAULT_BOARDS,
    assemble,
    derive_limits,
    drop_nonmonotonic_k,
    purge_dropped_symbols,
)
from a_share_quant.data.incremental import (  # noqa: E402
    KEY,
    atomic_write,
    check_continuity,
    upsert,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 读写
def _read_table(d: Path, name: str) -> pd.DataFrame:
    pq, csv = d / f"{name}.parquet", d / f"{name}.csv"
    if pq.exists():
        return pd.read_parquet(pq)
    if csv.exists():
        return pd.read_csv(csv, dtype={"symbol": str})
    raise SystemExit(f"缺少 {name}：{d} 下既没有 parquet 也没有 csv。")


def _read_calendar(d: Path) -> pd.DataFrame:
    p = d / "calendar.csv"
    if not p.exists():
        raise SystemExit(f"缺少 calendar.csv：{p}")
    cal = pd.read_csv(p)
    cal["trade_date"] = pd.to_datetime(cal["trade_date"]).dt.normalize()
    return cal


# ---------------------------------------------------------------- 主流程
def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="已有的数据目录（含四张表）")
    ap.add_argument("--source", choices=["gm", "baostock", "qmt"], default="gm")
    ap.add_argument("--overlap", type=int, default=5,
                    help="回退多少个交易日重取（覆盖数据源修订），默认 5")
    ap.add_argument("--end", default=None, help="截止日（默认今天）")
    ap.add_argument("--boards", default=",".join(DEFAULT_BOARDS))
    ap.add_argument("--refresh-instruments", action="store_true", default=True,
                    help="同时刷新证券列表（新上市/退市日期变化），默认开启")
    ap.add_argument("--no-refresh-instruments", dest="refresh_instruments",
                    action="store_false")
    ap.add_argument("--exempt-limit-breach", action="store_true",
                    help="显式将收盘价越过源表涨跌停的行改标为 limit_exempt=True（默认只告警不改值）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    args = ap.parse_args()

    d = Path(args.data_dir)
    if not d.is_absolute():
        d = PROJECT_ROOT / d
    if not (d / "bars.parquet").exists() and not (d / "bars.csv").exists():
        raise SystemExit(f"{d} 里没有 bars 表，请先用 build_dataset.py 全量构建一次。")

    print(f"[1/5] 读取现有数据 {d}")
    bars = _read_table(d, "bars")
    status = _read_table(d, "status")
    inst = _read_table(d, "instruments")
    cal = _read_calendar(d)
    for df in (bars, status):
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    bars["symbol"] = bars["symbol"].astype(str)
    status["symbol"] = status["symbol"].astype(str)
    inst["symbol"] = inst["symbol"].astype(str)

    last = bars["trade_date"].max()
    print(f"      现有 bars {len(bars):,} 行 / {bars['symbol'].nunique()} 只 / "
          f"截至 {last.date()}")

    source = {"gm": GmSource, "baostock": BaostockSource, "qmt": QmtSource}[args.source]()
    end = pd.Timestamp(args.end).normalize() if args.end else pd.Timestamp.today().normalize()

    # gm 的 history/get_history_instruments 需要「代码 -> 交易所」映射，
    # 正常由 instruments() 填充。增量更新不必再拉全市场截面——
    # instruments 表里已有 exchange 列，直接灌进去。
    if hasattr(source, "seed_exchange"):
        source.seed_exchange(dict(zip(inst["symbol"], inst["exchange"])))

    print(f"[2/5] 查询 {args.source} 交易日历 ...")
    lookback_start = (last - timedelta(days=args.overlap * 3 + 10)).date()
    try:
        src_cal = [pd.Timestamp(x).normalize()
                   for x in source.calendar(str(lookback_start), str(end.date()))]
    except Exception as exc:      # noqa: BLE001 - 网络/鉴权都要给出可读信息
        raise SystemExit(f"取交易日历失败：{type(exc).__name__}: {exc}") from exc

    new_days = [x for x in src_cal if x > last]
    if not new_days:
        print(f"      没有新交易日（源最新 {src_cal[-1].date() if src_cal else 'N/A'}，"
              f"本地 {last.date()}），无需更新。")
        return
    print(f"      新增 {len(new_days)} 个交易日："
          f"{new_days[0].date()} ~ {new_days[-1].date()}")

    # 回退 overlap 个交易日重取，用于覆盖数据源对最近几天的修订
    hist = sorted(x for x in src_cal if x <= last)
    start = hist[max(0, len(hist) - 1 - args.overlap)] if hist else last
    fetch_end = new_days[-1]
    print(f"      重取窗口 {start.date()} ~ {fetch_end.date()}"
          f"（含 {args.overlap} 日重叠）")

    fresh_inst = pd.DataFrame()
    if args.refresh_instruments:
        try:
            fresh_inst = source.instruments(str(start.date()), str(end.date()))
        except Exception as exc:      # noqa: BLE001
            print(f"[warn] 证券列表刷新失败，沿用旧的：{str(exc)[:60]}")

    symbols = sorted(inst["symbol"].unique())
    boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    print(f"[3/5] 拉取行情与状态（{len(symbols)} 只，{start.date()} 起）")
    cal_slice = [str(x.date()) for x in src_cal if x >= start]
    raw, post, status_new = source.fetch_all(
        symbols, str(start.date()), str(fetch_end.date()), calendar=cal_slice)
    if raw.empty:
        raise SystemExit("新窗口行情为空：检查账号权限或接口是否可用（不要写盘）。")
    print(f"      bars {len(raw):,} 行 / 状态 {len(status_new):,} 行")

    # 交易日历会把"今天"算进去，但收盘前拿不到今天的 bar。
    # 以**实际拿到的最大日期**为准，否则每天早上跑都会误报校验失败。
    got_last = pd.Timestamp(raw["trade_date"].max()).normalize()
    if got_last <= last:
        print(f"      源还没有比本地更新的行情（最新 {got_last.date()}），"
              f"可能今天尚未收盘，无需更新。")
        return
    if got_last < new_days[-1]:
        print(f"      [info] 源尚未提供 {new_days[-1].date()} 的行情（未收盘），"
              f"本次更新到 {got_last.date()}")

    print("[4/5] 装配新窗口")
    report = assemble(raw, post, status_new, inst,
                      calendar=cal_slice, boards=boards,
                      exempt_limit_breach=args.exempt_limit_breach)
    if report.bars["limit_up"].isna().all():
        print("      源未提供涨跌停价，按规则表推算")
        report.bars, w = derive_limits(report.bars, report.instruments, report.calendar)
        report.warnings.extend(w)

    print("[5/5] 合并与校验")
    bars2 = upsert(bars, report.bars)
    # 状态表必须用 assemble 的 status（来自源表），不能用 bars 推——
    # 停牌日通常没有行情行，从 bars 取会把停牌日整段丢掉，
    # 股票池的「剔除停牌」会静默失效。
    status_cols = [c for c in ("trade_date", "symbol", "is_st", "is_suspended")
                   if c in report.status.columns]
    # 源会给出"今天"的状态但拿不到今天的行情，截到 got_last，
    # 否则会出现"有状态、没行情"的孤儿日期。
    st_new = report.status[status_cols]
    st_new = st_new[pd.to_datetime(st_new["trade_date"]) <= got_last]
    status2 = upsert(status, st_new.drop_duplicates())
    new_cal_days = [x for x in new_days if x <= got_last]
    cal2 = (pd.concat([cal, pd.DataFrame({"trade_date": new_cal_days})], ignore_index=True)
            .drop_duplicates(subset=["trade_date"]).sort_values("trade_date")
            .reset_index(drop=True))

    bars2, dropped = drop_nonmonotonic_k(bars2)
    if dropped:
        drop_set = set(dropped)
        status2 = status2[~status2["symbol"].isin(drop_set)].reset_index(drop=True)
        inst = inst[~inst["symbol"].isin(drop_set)].reset_index(drop=True)
        if len(fresh_inst):
            fresh_inst = fresh_inst[~fresh_inst["symbol"].isin(drop_set)].reset_index(drop=True)
        print(f"[warn] 剔除 {len(dropped)} 只复权因子非单调的标的（已同步从 status/instruments 移除）：{dropped[:10]}")

    problems = []
    dup = bars2.duplicated(subset=KEY).sum()
    if dup:
        problems.append(f"合并后仍有 {dup} 条重复 (trade_date, symbol)")
    if bars2["trade_date"].max() < got_last:
        problems.append(f"合并后最新日期 {bars2['trade_date'].max().date()} "
                        f"早于源最新 {got_last.date()}")
    problems.extend(check_continuity(bars2))
    if problems:
        for p in problems:
            print(f"[error] {p}")
        raise SystemExit("增量校验未通过，**未写盘**。请检查数据源后重跑全量构建。")

    print(f"      bars {len(bars):,} -> {len(bars2):,} 行"
          f"（+{len(bars2) - len(bars):,}）")
    print(f"      status {len(status):,} -> {len(status2):,} 行")
    print(f"      calendar {len(cal):,} -> {len(cal2):,} 个交易日")

    # 基准指数同步补尾部
    bench_path = d / "benchmark.csv"
    if bench_path.exists():
        b = pd.read_csv(bench_path, dtype={"symbol": str})
        b["trade_date"] = pd.to_datetime(b["trade_date"]).dt.normalize()
        codes = b["symbol"].unique().tolist()
        parts = []
        for code in codes:
            try:
                nb, _ = source.index_bars(str(code), str(start.date()),
                                          str(got_last.date()))
            except Exception as exc:      # noqa: BLE001
                print(f"[warn] 基准 {code} 更新失败：{str(exc)[:60]}")
                continue
            if len(nb):
                parts.append(nb[["trade_date", "symbol", "close"]])
        if parts:
            bench2 = upsert(b, pd.concat(parts, ignore_index=True))
            if not args.dry_run:
                atomic_write(bench2, bench_path)
            print(f"      benchmark {len(b):,} -> {len(bench2):,} 行")

    if len(fresh_inst):
        # industry 是 fetch_industry.py 事后补的，源里没有——必须保留，
        # 否则一次增量更新就把行业归因能力抹掉。
        # 必须过板块白名单：get_symbols 返回的是全市场（含科创板/北交所），
        # 不过滤会一次增量就多出几百只不该入池的标的。
        from a_share_quant.data.builders import select_boards
        fresh_inst = select_boards(fresh_inst, boards)
        if "industry" in inst.columns:
            fresh_inst = fresh_inst.merge(inst[["symbol", "industry"]], on="symbol",
                                          how="left")
        only_old = inst[~inst["symbol"].isin(fresh_inst["symbol"])]
        inst2 = pd.concat([only_old, fresh_inst], ignore_index=True)
        print(f"      instruments {len(inst):,} -> {len(inst2):,} 只")
    else:
        inst2 = inst

    if args.dry_run:
        print("\n--dry-run：未写盘。")
        return

    atomic_write(bars2, d / "bars.parquet")
    atomic_write(status2, d / "status.parquet")
    atomic_write(inst2, d / "instruments.parquet")
    atomic_write(cal2, d / "calendar.csv")
    if hasattr(source, "close"):
        source.close()
    print(f"\n[done] {d}")
    print(f"  bars 截至 {bars2['trade_date'].max().date()}"
          f"，{bars2['symbol'].nunique():,} 只")
    print(f"  updated_at {datetime.now():%Y-%m-%d %H:%M:%S}")
    # 数据一变，data/MANIFEST.json 里这份数据集的指纹就过期了。不自动改写：
    # 指纹的用途是"钉住某一版数据"，每天跟着数据走就等于没有指纹。
    if (PROJECT_ROOT / "data" / "MANIFEST.json").exists():
        print(f"  [note] 数据已变化，指纹未自动更新；钉住这一版再跑：\n"
              f"         python scripts/data_manifest.py write --data-dir {d}")


if __name__ == "__main__":
    sys.exit(main())
