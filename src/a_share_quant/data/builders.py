"""把外部数据源（掘金 / QMT）的原始表装配成引擎认识的四张表。

设计原则：
  * 这一层是**纯函数**，不 import 任何 SDK，便于离线单测；
  * SDK 调用留在 ``scripts/build_dataset.py`` 的 Source 适配器里；
  * 硬校验不过就 raise，不产出"看起来能跑"的数据集。

产出四张表（列名与 ``data/schema.py`` 对齐）：
  bars         trade_date, symbol, open, high, low, close, volume, amount,
               pre_close, adj_close, limit_up, limit_down, limit_exempt,
               is_st, is_suspended
  instruments  symbol, exchange, name, list_date, delist_date
  status       trade_date, symbol, is_st, is_suspended
  calendar     trade_date
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import pandas as pd

from .validation import DataValidationError

# 板块判定：(交易所, 代码前缀) -> 板块
_BOARD_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("SHSE", ("600", "601", "603", "605"), "main_sh"),
    ("SHSE", ("688", "689"), "star"),
    ("SZSE", ("000", "001", "002", "003"), "main_sz"),
    ("SZSE", ("300", "301"), "chinext"),
    ("BJSE", ("43", "83", "87", "92"), "bse"),
)

BOARD_ALIASES = {
    "main": ("main_sh", "main_sz"),
    "main_sh": ("main_sh",),
    "main_sz": ("main_sz",),
    "chinext": ("chinext",),
    "star": ("star",),
    "bse": ("bse",),
}

DEFAULT_BOARDS = ("main_sh", "main_sz", "chinext")


def split_symbol(raw: str) -> tuple[str, str]:
    """把 ``SHSE.600000`` / ``600000.SH`` / ``600000`` 拆成 (代码, 交易所)。"""
    text = str(raw).strip().upper()
    if "." in text:
        left, right = text.split(".", 1)
        if left in ("SHSE", "SZSE", "BJSE", "SH", "SZ", "BJ"):
            return right, _norm_exchange(left)
        return left, _norm_exchange(right)
    return text, ""


def _norm_exchange(tag: str) -> str:
    return {"SH": "SHSE", "SZ": "SZSE", "BJ": "BJSE"}.get(tag, tag)


def classify_board(symbol: str, exchange: str = "") -> str:
    """按交易所 + 代码前缀判定板块；判定不出返回 ``unknown``。"""
    sym = str(symbol)
    ex = _norm_exchange(str(exchange).upper()) if exchange else ""
    for rule_ex, prefixes, board in _BOARD_RULES:
        if ex and rule_ex != ex:
            continue
        if sym.startswith(prefixes):
            return board
    # 交易所缺失时按前缀唯一映射（SHSE/SZSE 的 000 前缀会冲突，只能靠 exchange）
    return "unknown"


def is_index(symbol: str, exchange: str = "") -> bool:
    """粗判指数：沪市 000xxx、深市 399xxx。"""
    sym, ex = str(symbol), _norm_exchange(str(exchange).upper())
    return (ex == "SHSE" and sym.startswith("000")) or \
           (ex == "SZSE" and sym.startswith("399"))


def select_boards(instruments: pd.DataFrame, boards: Sequence[str]) -> pd.DataFrame:
    """按板块白名单筛证券。``boards`` 用 ``BOARD_ALIASES`` 的键。"""
    wanted: set[str] = set()
    for b in boards:
        if b not in BOARD_ALIASES:
            raise ValueError(f"unknown board {b!r}, expected one of {list(BOARD_ALIASES)}")
        wanted.update(BOARD_ALIASES[b])
    out = instruments.copy()
    out["board"] = [classify_board(s, e) for s, e in zip(out["symbol"], out["exchange"])]
    out = out[out["board"].isin(wanted)]
    return out.drop(columns=["board"]).reset_index(drop=True)


@dataclass
class BuildReport:
    bars: pd.DataFrame
    instruments: pd.DataFrame
    status: pd.DataFrame
    calendar: pd.DataFrame
    warnings: list[str]


def _as_date(df: pd.DataFrame, col: str = "trade_date") -> pd.DataFrame:
    out = df.copy()
    out[col] = pd.to_datetime(out[col]).dt.normalize()
    return out


def assemble(bars_raw: pd.DataFrame, bars_post: pd.DataFrame,
             status_raw: pd.DataFrame, instruments: pd.DataFrame,
             calendar: Sequence, boards: Sequence[str] = DEFAULT_BOARDS,
             limit_symbols: Iterable[str] | None = None,
             extra_symbols: Iterable[str] | None = None) -> BuildReport:
    """装配四张表。

    参数
    ----
    bars_raw   原始价行情：trade_date, symbol, open, high, low, close, volume, amount, pre_close
    bars_post  后复权行情：trade_date, symbol, adj_close
    status_raw 逐日状态：trade_date, symbol, is_st, is_suspended, limit_up, limit_down
    instruments symbol, exchange, name, list_date, delist_date
    calendar   交易日序列
    boards     板块白名单
    limit_symbols 只保留这些代码（小样本调试用）
    extra_symbols 额外保留的代码（基准指数）：**只留在 bars 里做对照**，
                  不进 instruments，因此不会被股票池选中、也不会参与交易
    """
    warnings: list[str] = []
    inst = instruments.copy()
    inst["symbol"] = inst["symbol"].astype(str)
    inst["exchange"] = inst.get("exchange", "").astype(str) if "exchange" in inst else ""
    if "list_date" in inst.columns:
        inst["list_date"] = pd.to_datetime(inst["list_date"])
    if "delist_date" in inst.columns:
        inst["delist_date"] = pd.to_datetime(inst["delist_date"])

    # 指数（如 SHSE.000300）不是可交易证券，先剔除；板块白名单再筛一次
    index_mask = [is_index(s, e) for s, e in zip(inst["symbol"], inst["exchange"])]
    if any(index_mask):
        warnings.append(f"instruments 中剔除 {sum(index_mask)} 个指数代码（如 SHSE.000300）")
        inst = inst[~pd.Series(index_mask, index=inst.index)]
    inst = select_boards(inst, boards)

    keep = set(inst["symbol"])
    if limit_symbols is not None:
        keep &= {str(s) for s in limit_symbols}
    if not keep:
        raise DataValidationError("筛选后没有任何证券，请检查 --boards / --limit-symbols")

    bars = _as_date(bars_raw)
    bars["symbol"] = bars["symbol"].astype(str)
    keep_bars = keep | {str(s) for s in (extra_symbols or [])}
    bars = bars[bars["symbol"].isin(keep_bars)]
    adj = _as_date(bars_post)
    adj["symbol"] = adj["symbol"].astype(str)
    adj = adj[adj["symbol"].isin(keep_bars)]
    bars = bars.merge(adj[["trade_date", "symbol", "adj_close"]],
                      on=["trade_date", "symbol"], how="left")

    status = _as_date(status_raw)
    status["symbol"] = status["symbol"].astype(str)
    status = status[status["symbol"].isin(keep)].copy()

    # 涨跌停价 <= 0 表示当日无涨跌幅限制（新股/特殊情形）：
    # 记 limit_exempt=True 并把价格置空，执行层据此跳过约束。
    # 注意：**缺失（NaN）与"无限制"是两回事**——NaN 不豁免，交给自检报错。
    for col in ("limit_up", "limit_down"):
        if col not in status.columns:
            status[col] = float("nan")
        status[col] = pd.to_numeric(status[col], errors="coerce")
    no_limit = (status["limit_up"] <= 0) | (status["limit_down"] <= 0)
    status.loc[no_limit, ["limit_up", "limit_down"]] = float("nan")
    status["limit_exempt"] = no_limit

    merged = bars.merge(
        status[["trade_date", "symbol", "is_st", "is_suspended",
                "limit_up", "limit_down", "limit_exempt"]],
        on=["trade_date", "symbol"], how="left")

    # 有 bar 但在状态表里找不到 -> 视为数据缺口，严格标记以便自检拦截
    missing_status = int(merged["limit_exempt"].isna().sum())
    if missing_status:
        warnings.append(
            f"{missing_status} 行行情在状态表中没有对应记录（涉及 "
            f"{merged.loc[merged['limit_exempt'].isna(), 'symbol'].nunique()} 只证券）。"
            f"状态表必须逐日覆盖，否则 ST/停牌/涨跌停都会失真。")
    for col in ("is_st", "is_suspended"):
        merged[col] = merged[col].fillna(False).astype(bool)
    merged["limit_exempt"] = merged["limit_exempt"].fillna(False).astype(bool)

    # 源表（如 gm）在退市整理期首日或恢复上市首日仍可能给出 ±10% 的名义涨跌停价，
    # 但当日实际不设涨跌幅，真实收盘价已越过该名义涨跌停（如 2025-2026 的 13 只退市整理期首日暴跌 -50%~-91%）。
    # 若保留该名义跌停价，执行层会把当日正常交易误判为跌停封死（blocked:limit_down）。
    close_num = pd.to_numeric(merged["close"], errors="coerce")
    lu_num = pd.to_numeric(merged["limit_up"], errors="coerce")
    ld_num = pd.to_numeric(merged["limit_down"], errors="coerce")
    breach = (~merged["limit_exempt"]) & (
        (lu_num.notna() & (close_num > lu_num + 0.011))
        | (ld_num.notna() & (close_num < ld_num - 0.011))
    )
    if breach.any():
        n_b = int(breach.sum())
        syms_b = sorted(merged.loc[breach, "symbol"].unique().tolist())[:5]
        warnings.append(
            f"{n_b} 行行情的收盘价越过源表给出的涨跌停价（例如 {syms_b}，"
            f"多为退市整理期首日或恢复上市首日无涨跌幅限制），已改标 limit_exempt=True。")
        merged.loc[breach, ["limit_up", "limit_down"]] = float("nan")
        merged.loc[breach, "limit_exempt"] = True

    # 基准指数不是可交易证券：没有涨跌停，也不该被自检当成"缺涨跌停价"
    if extra_symbols:
        idx_mask = merged["symbol"].isin({str(s) for s in extra_symbols})
        if idx_mask.any():
            merged.loc[idx_mask, ["limit_up", "limit_down"]] = float("nan")
            merged.loc[idx_mask, "limit_exempt"] = True
            merged.loc[idx_mask, ["is_st", "is_suspended"]] = False

    if "pre_close" not in merged.columns:
        merged["pre_close"] = merged.groupby("symbol")["close"].shift(1)

    bars_out = merged.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume", "amount",
                "pre_close", "adj_close", "limit_up", "limit_down"):
        if col in bars_out.columns:
            bars_out[col] = pd.to_numeric(bars_out[col], errors="coerce")
    bars_out = bars_out.loc[:, [
        "trade_date", "symbol", "open", "high", "low", "close", "volume",
        "amount", "pre_close", "adj_close", "limit_up", "limit_down",
        "limit_exempt", "is_st", "is_suspended"]]

    # 状态表直接来自源表，不经过 bars 合并——停牌日往往没有行情行，
    # 若从 merged 取会把这些天丢掉，股票池的"剔除停牌"就失效了。
    status_out = status[["trade_date", "symbol", "is_st", "is_suspended"]] \
        .drop_duplicates() \
        .sort_values(["trade_date", "symbol"]).reset_index(drop=True)

    cal = pd.DataFrame({"trade_date": pd.to_datetime(list(calendar))}).drop_duplicates() \
        .sort_values("trade_date").reset_index(drop=True)

    inst_out = inst.loc[:, [c for c in
                            ("symbol", "exchange", "name", "list_date", "delist_date")
                            if c in inst.columns]].reset_index(drop=True)

    return BuildReport(bars_out, inst_out, status_out, cal, warnings)


# ---------------- 清理 ----------------
def drop_nonmonotonic_k(bars: pd.DataFrame,
                        tol: float | None = None) -> tuple[pd.DataFrame, list[str]]:
    """剔除复权因子非单调的标的，返回 (清洗后 bars, 被剔除代码)。

    后复权因子 ``k = adj_close / close`` 必须单调不减。出现下降通常意味着
    数据源把因子**重置**了——实测 gm 在 2018-12-26 把 000022 的因子从 3.53
    直接写成 1.00（当日价格没有跳空）。这种重置会让该股在重置日凭空跌 71%，
    之后的收益也全部错位。

    无法可靠修复（不知道正确因子），只能剔除。剔 1~2 只对全样本影响可忽略，
    留着则会让 IC 与回测出现剧烈假信号。
    """
    tol = K_TOL if tol is None else float(tol)
    if bars["adj_close"].isna().all():
        return bars, []
    k = (bars["adj_close"] / bars["close"]).replace([float("inf")], pd.NA)
    frame = pd.DataFrame({"symbol": bars["symbol"], "trade_date": bars["trade_date"],
                          "k": k}).dropna()
    frame = frame.sort_values(["symbol", "trade_date"])
    diff = frame.groupby("symbol")["k"].diff()
    offenders = sorted(str(x) for x in frame.loc[diff < -tol, "symbol"].unique())
    if not offenders:
        return bars, []
    return bars[~bars["symbol"].isin(offenders)], offenders


def purge_dropped_symbols(report: BuildReport,
                          tol: float | None = None) -> tuple[BuildReport, list[str]]:
    """剔除复权因子非单调的标的，并同步从 instruments 与 status 中移除。

    若只从 bars 剔除而留在 instruments/status 中（如 2015-2019 的 000022/000043），
    UniverseBuilder 仍会把它们当作可投资标的选入股票池，导致下单时触发 blocked:no_bar。
    """
    kept_bars, dropped = drop_nonmonotonic_k(report.bars, tol=tol)
    if not dropped:
        return report, []
    drop_set = set(dropped)
    kept_inst = report.instruments[~report.instruments["symbol"].isin(drop_set)].reset_index(drop=True)
    kept_status = report.status[~report.status["symbol"].isin(drop_set)].reset_index(drop=True)
    warns = list(report.warnings)
    warns.append(
        f"剔除 {len(dropped)} 只复权因子非单调的标的（已同步从 instruments/status 移除）："
        f"{dropped[:10]}{'...' if len(dropped) > 10 else ''}")
    return BuildReport(kept_bars.reset_index(drop=True), kept_inst, kept_status,
                       report.calendar, warns), dropped


# ---------------- 硬校验 ----------------

K_TOL = 1e-6

# 创业板注册制改革：2020-08-24 起涨跌幅由 10% 放宽到 20%
CHINEXT_REFORM = pd.Timestamp("2020-08-24")
# 沪深主板风险警示板（ST/*ST）规则调整：2026-07-06 起涨跌幅由 5% 调整为 10%
MAIN_ST_REFORM = pd.Timestamp("2026-07-06")
NEW_LISTING_EXEMPT_DAYS = 5   # 注册制新股上市前 5 个交易日无涨跌幅限制


def limit_ratio(symbol: str, exchange: str, trade_date, is_st: bool) -> float | None:
    """当日涨跌幅比例。返回 ``None`` 表示当日无涨跌幅限制（由调用方按上市天数判断）。

    规则表（只覆盖本项目允许的板块）：
      * 主板（沪 600/601/603/605、深 000/001/002/003）：10%，ST 在 2026-07-06 前 5%、之后 10%
      * 创业板（300/301）：2020-08-24 起 20%，之前 10%（ST 同样 20%）
      * 科创板 20%、北交所 30%（默认白名单不含，仅兜底）
    """
    board = classify_board(symbol, exchange)
    ts = pd.Timestamp(trade_date)
    if board == "main_sh" or board == "main_sz":
        if is_st and ts < MAIN_ST_REFORM:
            return 0.05
        return 0.10
    if board == "chinext":
        return 0.20 if ts >= CHINEXT_REFORM else 0.10
    if board == "star":
        return 0.20
    if board == "bse":
        return 0.30
    return None


def derive_limits(bars: pd.DataFrame, instruments: pd.DataFrame,
                  calendar: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """按规则表生成 ``limit_up / limit_down / limit_exempt``。

    数据源（baostock / QMT）不给历史涨跌停价，只能按交易所规则推算。
    推算结果用**当日收盘价不得越过涨跌停**来自检——表写错（例如把 ST 当普通股）
    会立刻暴露，而不是悄悄放宽约束。

    已知近似（写进 README 限制清单）：
      * 注册制新股上市前 5 个交易日无涨跌幅 -> ``limit_exempt=True``
      * 退市整理期首日无涨跌幅，本函数按普通涨跌幅处理
    """
    warnings: list[str] = []
    out = bars.copy()
    cal = pd.DatetimeIndex(sorted(pd.to_datetime(calendar["trade_date"]).unique()))
    cal_pos = {d: i for i, d in enumerate(cal)}

    need = out[["trade_date", "symbol", "is_st"]].copy()
    inst = instruments.set_index("symbol")
    list_dates = inst["list_date"] if "list_date" in inst.columns else pd.Series(dtype="datetime64[ns]")

    ratios, exempt = [], []
    for sym, ts, is_st in zip(need["symbol"], need["trade_date"], need["is_st"]):
        ex = inst["exchange"].get(sym, "") if "exchange" in inst.columns else ""
        r = limit_ratio(sym, ex, ts, bool(is_st))
        if r is None:
            exempt.append(True)
            ratios.append(r)
            continue
        ld = list_dates.get(sym, pd.NaT) if len(list_dates) else pd.NaT
        if pd.notna(ld) and (ts - pd.Timestamp(ld)).days <= 10:
            # 上市 10 个自然日内，用交易日序号判断是否处于"前 5 日无限制"窗口
            i_ts, i_ld = cal_pos.get(pd.Timestamp(ts)), cal_pos.get(pd.Timestamp(ld))
            if i_ts is not None and i_ld is not None and i_ts - i_ld < NEW_LISTING_EXEMPT_DAYS:
                exempt.append(True)
                ratios.append(r)
                continue
        exempt.append(False)
        ratios.append(r)

    out["_ratio"] = ratios
    out["limit_exempt"] = exempt
    pre = pd.to_numeric(out["pre_close"], errors="coerce")
    out["limit_up"] = (pre * (1 + out["_ratio"])).round(2)
    out["limit_down"] = (pre * (1 - out["_ratio"])).round(2)
    out.loc[out["limit_exempt"], ["limit_up", "limit_down"]] = float("nan")

    # 自检：收盘价不得越过推算出的涨跌停（容忍 1 分钱取整）
    live = ~out["limit_exempt"]
    over_up = live & (out["close"] > out["limit_up"] + 0.011)
    over_down = live & (out["close"] < out["limit_down"] - 0.011)
    n_bad = int((over_up | over_down).sum())
    if n_bad:
        sample = out.loc[over_up | over_down, ["trade_date", "symbol", "close",
                                               "limit_up", "limit_down"]].head(3)
        raise DataValidationError(
            f"{n_bad} 行的收盘价超出按规则表推算的涨跌停（例如 "
            f"{sample.to_dict('records')}）。说明涨跌幅规则表或 is_st 标记有误，"
            "不要带着错误的涨跌停价跑回测。")

    n_exempt = int(out["limit_exempt"].sum())
    if n_exempt:
        warnings.append(
            f"{n_exempt} 行按「无涨跌幅限制」处理（注册制新股上市前 "
            f"{NEW_LISTING_EXEMPT_DAYS} 个交易日），这些行不参与涨跌停拦截")
    out = out.drop(columns=["_ratio"])
    return out, warnings


def self_check(report: BuildReport, allow_missing_limits: bool = False,
               min_symbols_per_day: int = 100) -> list[str]:
    """六条硬校验。不通过直接 raise，返回可容忍的告警列表。"""
    warnings: list[str] = list(report.warnings)
    bars, inst, cal = report.bars, report.instruments, report.calendar

    # 1) symbol 必须是字符串且 6 位
    if bars["symbol"].dtype != object and not pd.api.types.is_string_dtype(bars["symbol"]):
        raise DataValidationError("bars.symbol 不是字符串类型，前导零已丢失")
    bad = bars.loc[~bars["symbol"].str.fullmatch(r"\d{6}"), "symbol"].unique()
    if len(bad):
        raise DataValidationError(
            f"bars.symbol 有 {len(bad)} 个不是 6 位数字（例如 {list(bad)[:5]}）")

    # 2) 退市股必须存在（幸存者偏差的最低保障）
    if "delist_date" not in inst.columns or inst["delist_date"].notna().sum() == 0:
        raise DataValidationError(
            "instruments 中没有任何已退市证券。这样的数据集会产生幸存者偏差，"
            "必须包含退市股（list_status/已退市标记）。")

    # 3) 涨跌停价覆盖
    if not allow_missing_limits:
        missing = bars["limit_up"].isna() | bars["limit_down"].isna()
        missing &= ~bars["limit_exempt"].astype(bool)
        if missing.any():
            raise DataValidationError(
                f"{int(missing.sum())} 行行情缺少涨跌停价且未标记 limit_exempt。"
                "请补数据，或用 --allow-missing-limits 并在配置里关闭 "
                "market_rules.consider_limit_up_down。")

    # 4) 复权因子 k = adj_close/close 必须单调不减（后复权的判据）
    if bars["adj_close"].notna().any():
        k = (bars["adj_close"] / bars["close"]).replace([float("inf")], pd.NA)
        frame = pd.DataFrame({"symbol": bars["symbol"], "trade_date": bars["trade_date"],
                              "k": k}).dropna()
        frame = frame.sort_values(["symbol", "trade_date"])
        drop = frame.groupby("symbol")["k"].diff()
        offenders = frame.loc[drop < -K_TOL, "symbol"].unique()
        if len(offenders):
            raise DataValidationError(
                f"{len(offenders)} 只证券的 adj_close/close 出现下降（例如 "
                f"{list(offenders)[:5]}）。后复权因子应单调不减——这通常意味着"
                "数据源给的是前复权，或复权价与原始价没有按同一时点对齐。")

    # 5) 每个交易日不该只剩零星几只（拉取中断的典型症状）
    per_day = bars.groupby("trade_date")["symbol"].nunique()
    thin = per_day[per_day < min_symbols_per_day]
    if len(thin):
        warnings.append(
            f"{len(thin)} 个交易日的证券数少于 {min_symbols_per_day}"
            f"（最少的 {int(thin.min())} 只，例如 {thin.idxmin().date()}），"
            f"请确认是否拉取中断；小样本调试可用 --min-symbols-per-day 1")

    # 6) 停牌标记与行情是否自洽
    susp = bars[bars["is_suspended"].astype(bool)]
    if len(susp):
        traded = susp[susp["volume"].fillna(0) > 0]
        if len(traded):
            warnings.append(
                f"{len(traded)} 行标了停牌却仍有成交量（例如 "
                f"{traded['symbol'].iloc[0]} {traded['trade_date'].iloc[0].date()}），"
                f"请确认 is_suspended 的定义")

    # 日历覆盖
    outside = bars.loc[~bars["trade_date"].isin(set(cal["trade_date"])), "trade_date"]
    if len(outside):
        raise DataValidationError(
            f"{len(outside)} 行行情的日期不在交易日历中（例如 {outside.iloc[0].date()}）")

    return warnings
