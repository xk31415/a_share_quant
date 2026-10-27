"""炒作苗头 + 观察池：paper / 观察仿真，不是买卖建议。

策略类的 ``paper_only`` 必须为 true。回测只产纸面权重。
仿真账户下单不在这里做，而在 ``scripts/live_signal_hype.py``
（``live_auto`` 仅当 ``config/live.yaml`` 的 ``strategy`` 为 ``hype_watch`` 时调用）。
那条链路不代表规则已通过研究判定。

信号（T0 收盘，只用 ``trade_date <= T0`` 的数据）
------------------------------------------------
一条题材触发，当且仅当下面几条同时成立：

1. 当日可交易成分（股票池 ∩ 主板/创业板 ∩ 非 ST ∩ 非停牌）里，
   收盘价达到涨停价的家数 ``>= min_limitup_count``。
   涨停以 ``bars.limit_up`` 为准，不用 ``pre_close * 10%`` 推算。
   任一可交易成分缺涨停价 → 该题材当日不触发（``missing_limit``），
   不会把缺数据当成「没涨停」。
2. 这些成分用后复权收盘价算出的等权日收益，减去基准（默认中证 1000，
   ``000852``，与 ``settings.yaml`` 和 ``benchmark.csv`` 一致；数据集里没有沪深 300）
   的日收益，``>= min_theme_excess``。门槛为 ``None`` 时不做这一条。
3. 能算出收益的成分不少于 ``min_theme_members``。

触发题材按（涨停家数降序，超额降序，题材名升序）最多保留 ``max_themes`` 条
（1–3）。观察池最多 ``pool_size`` 只（默认 3）：

* 纯规则：每个题材里取收盘涨停的成分，按成交额降序、代码升序，再按题材轮转填满；
* 晨报 CSV：当日名单里的代码，题材必须已触发（``csv_require_trigger=true``）。
  ``csv_require_trigger=false`` 只回放名单，涨停与超额门槛不生效，日志写明。

观察窗口 T+1 / T+3 / T+5 的收盘收益由 :func:`build_observations` 在回测结束后
计算，不进 :meth:`HypeWatchStrategy.generate_target`。

成交
----
引擎在下一交易日按 ``execution_price``（``open`` 或 ``close``）撮合，即 shift(1)。
仓位 ``equal``：入选标的平分 ``gross_exposure``。``fixed``：每只 ``fixed_weight``，
合计超过 ``gross_exposure`` 时缩到该上限，不放大。

断路（可选）
------------
信号日收盘权益相对近 ``circuit_lookback`` 个交易日峰值的回撤
``<= circuit_max_drawdown``（默认 -8%）时，当日及随后若干个信号日目标为空仓。
窗口是滚动峰值，不是从回测起点起的历史高点。冷静期按信号日计数；
日频时才等于 N 个交易日。权益里晚于 as_of 的点会被丢掉。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import pandas as pd

from ..backtest.costs import CostModel
from .base import Strategy, TargetPortfolio

# 沪深主板 + 创业板。科创板 688/689、北交所、B 股不在这里。
# 这是任务约束，不放进 yaml，避免配置把板块放宽。
MAIN_CHINEXT_PREFIXES: tuple[str, ...] = (
    "600", "601", "603", "605",
    "000", "001", "002", "003",
    "300", "301",
)

PAPER_LABEL = "paper/观察仿真"
PAPER_DISCLAIMER = (
    "PAPER / 观察仿真。本输出只记录规则触发与仿真净值，不是买卖建议。"
    "策略类 paper_only 必须为 true。仿真账户若下单，只经 "
    "scripts/live_signal_hype.py，不代表规则已通过研究判定。"
    "观察窗口收益是事后研究指标，不代表策略有效，未写入研究判定。"
)

_SYMBOL_RE = re.compile(r"\d{6}")
_TOP_KEYS = {
    "paper_only", "input_mode", "pool_size", "max_themes",
    "min_limitup_count", "min_theme_excess", "min_theme_members",
    "observe_horizons", "position_mode", "gross_exposure", "fixed_weight",
    "execution_price", "rebalance_frequency", "benchmark", "limitup_abs_tol",
    "csv_require_trigger", "circuit_breaker", "costs",
}
_CIRCUIT_KEYS = {"enabled", "lookback", "max_drawdown", "cooldown_days"}
_COST_KEYS = {"commission_rate", "commission_min", "slippage_rate", "stamp_duty_rate"}


def _as_bool(value) -> bool:
    """缺省与 NaN 都是 False。``bool(float('nan'))`` 会得到 True，不能用。"""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "t", "yes", "y", "是"}
    return bool(value)


def _require_symbols(series: pd.Series, what: str) -> pd.Series:
    sym = series.astype(str).str.strip()
    if sym.str.endswith(".0").any():
        raise ValueError(
            f"{what} 的 symbol 带有 .0，像是被读成了数字。请把代码按字符串保存。")
    bad = sorted(set(sym[~sym.str.fullmatch(r"\d{6}")].tolist()))
    if bad:
        raise ValueError(
            f"{what} 的 symbol 必须是 6 位数字且保留前导零，"
            f"例如 {bad[:5]}。不会自动补零。")
    return sym


@dataclass(frozen=True)
class HypeWatchParams:
    """先验参数。不按收益搜索；改默认值要改测试里锁住的 settings.yaml。"""

    paper_only: bool = True
    input_mode: str = "auto"          # auto | rules | csv
    pool_size: int = 3
    max_themes: int = 3
    min_limitup_count: int = 3
    min_theme_excess: Optional[float] = 0.01
    min_theme_members: int = 3
    observe_horizons: tuple[int, ...] = (1, 3, 5)
    position_mode: str = "equal"      # equal | fixed
    gross_exposure: float = 1.0
    fixed_weight: float = 0.20
    execution_price: str = "open"     # 下一交易日撮合列
    rebalance_frequency: str = "D"
    benchmark: str = "000852"         # 中证1000；与 settings.yaml / benchmark.csv 一致
    limitup_abs_tol: float = 1e-4
    csv_require_trigger: bool = True
    circuit_enabled: bool = True
    circuit_lookback: int = 20
    circuit_max_drawdown: float = -0.08
    circuit_cooldown_days: int = 5
    commission_rate: float = 0.00025
    commission_min: float = 5.0
    slippage_rate: float = 0.00075
    stamp_duty_rate: float = 0.0005

    def __post_init__(self) -> None:
        if self.paper_only is not True:
            raise ValueError(
                "hype_watch 只允许 paper_only=true。"
                "没有实盘接线，也不能把本策略接到 live_signal / live_auto。")
        if self.input_mode not in ("auto", "rules", "csv"):
            raise ValueError("input_mode 必须是 auto、rules 或 csv")
        if not 1 <= int(self.pool_size) <= 5:
            raise ValueError("pool_size 必须在 1..5（观察池，不是宽基持仓）")
        if not 1 <= int(self.max_themes) <= 3:
            raise ValueError("max_themes 必须在 1..3")
        if int(self.min_limitup_count) < 1:
            raise ValueError("min_limitup_count 必须 >= 1")
        if int(self.min_theme_members) < 1:
            raise ValueError("min_theme_members 必须 >= 1")
        if self.min_theme_excess is not None and not -1.0 < float(self.min_theme_excess) < 5.0:
            raise ValueError("min_theme_excess 用小数（1% 写成 0.01），或 null 关闭")
        horizons = tuple(int(h) for h in self.observe_horizons)
        if not horizons or any(h < 1 for h in horizons):
            raise ValueError("observe_horizons 必须是正整数")
        object.__setattr__(self, "observe_horizons", horizons)
        if self.position_mode not in ("equal", "fixed"):
            raise ValueError("position_mode 必须是 equal 或 fixed")
        if not 0.0 < float(self.gross_exposure) <= 1.0:
            raise ValueError("gross_exposure 必须在 (0, 1]")
        if not 0.0 < float(self.fixed_weight) <= 1.0:
            raise ValueError("fixed_weight 必须在 (0, 1]")
        if self.execution_price not in ("open", "close"):
            raise ValueError("execution_price 必须是 open 或 close")
        if self.rebalance_frequency not in ("D", "W", "M"):
            raise ValueError("rebalance_frequency 必须是 D、W 或 M")
        if not 0.0 <= float(self.limitup_abs_tol) < 0.01:
            raise ValueError("limitup_abs_tol 必须在 [0, 0.01)，单位是元")
        if self.circuit_enabled:
            if int(self.circuit_lookback) < 2:
                raise ValueError("circuit_lookback 必须 >= 2（否则滚动峰值回撤恒为 0）")
            dd = float(self.circuit_max_drawdown)
            if not -1.0 < dd < 0.0:
                raise ValueError(
                    "circuit_max_drawdown 必须在 (-1, 0)，-8% 写成 -0.08，不要写 -8")
            if int(self.circuit_cooldown_days) < 1:
                raise ValueError("circuit_cooldown_days 必须 >= 1")
        # 成本范围交给 CostModel，避免两套校验不一致。
        self.cost_model()

    def cost_model(self) -> CostModel:
        return CostModel(
            commission_rate=float(self.commission_rate),
            commission_min=float(self.commission_min),
            stamp_duty_rate=float(self.stamp_duty_rate),
            slippage_rate=float(self.slippage_rate))

    def one_way_friction(self) -> float:
        """比例部分：佣金 + 滑点。不含最低佣金，也不含卖出印花税。"""
        return float(self.commission_rate) + float(self.slippage_rate)

    def excess_gate_active(self, mode: str) -> bool:
        if self.min_theme_excess is None:
            return False
        if mode == "csv" and not self.csv_require_trigger:
            return False
        return True

    @classmethod
    def from_mapping(cls, raw: Mapping | None) -> "HypeWatchParams":
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError("settings.yaml 缺少 hype_watch 段")
        unknown = set(raw) - _TOP_KEYS
        if unknown:
            raise ValueError(f"hype_watch 有未知键 {sorted(unknown)}，请改参数名而不是忽略")
        cb = dict(raw.get("circuit_breaker") or {})
        costs = dict(raw.get("costs") or {})
        bad_cb = set(cb) - _CIRCUIT_KEYS
        bad_cost = set(costs) - _COST_KEYS
        if bad_cb or bad_cost:
            raise ValueError(
                f"hype_watch 嵌套段有未知键 circuit={sorted(bad_cb)} costs={sorted(bad_cost)}")
        horizons = raw.get("observe_horizons", (1, 3, 5))
        kwargs = dict(
            paper_only=raw.get("paper_only", True),
            input_mode=str(raw.get("input_mode", "auto")),
            pool_size=int(raw.get("pool_size", 3)),
            max_themes=int(raw.get("max_themes", 3)),
            min_limitup_count=int(raw.get("min_limitup_count", 3)),
            min_theme_excess=raw.get("min_theme_excess", 0.01),
            min_theme_members=int(raw.get("min_theme_members", 3)),
            observe_horizons=tuple(int(h) for h in horizons),
            position_mode=str(raw.get("position_mode", "equal")),
            gross_exposure=float(raw.get("gross_exposure", 1.0)),
            fixed_weight=float(raw.get("fixed_weight", 0.20)),
            execution_price=str(raw.get("execution_price", "open")),
            rebalance_frequency=str(raw.get("rebalance_frequency", "D")),
            benchmark=str(raw.get("benchmark", "000852")),
            limitup_abs_tol=float(raw.get("limitup_abs_tol", 1e-4)),
            csv_require_trigger=_require_bool(
                raw.get("csv_require_trigger", True), "csv_require_trigger"),
            circuit_enabled=_require_bool(
                cb.get("enabled", True), "circuit_breaker.enabled"),
            circuit_lookback=int(cb.get("lookback", 20)),
            circuit_max_drawdown=float(cb.get("max_drawdown", -0.08)),
            circuit_cooldown_days=int(cb.get("cooldown_days", 5)),
        )
        if costs:
            for key in _COST_KEYS:
                if key in costs and costs[key] is not None:
                    kwargs[key] = float(costs[key])
        if kwargs["min_theme_excess"] is not None:
            kwargs["min_theme_excess"] = float(kwargs["min_theme_excess"])
        kwargs["paper_only"] = _require_bool(raw.get("paper_only", True), "paper_only")
        return cls(**kwargs)


def _require_bool(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise TypeError(f"{name} 必须是布尔值 true/false，收到 {value!r}")


def resolve_input_mode(params: HypeWatchParams, watchlist: pd.DataFrame | None) -> str:
    mode = params.input_mode
    if mode == "auto":
        return "csv" if watchlist is not None else "rules"
    if mode == "rules" and watchlist is not None:
        raise ValueError(
            "input_mode=rules 时不要同时传晨报 CSV。"
            "回放名单请把 input_mode 设为 csv 或 auto。")
    if mode == "csv" and watchlist is None:
        raise ValueError("input_mode=csv 但没有晨报 CSV（--watchlist）")
    return mode


def load_watchlist_csv(path) -> pd.DataFrame:
    """晨报 / 观察池名单。列：trade_date, symbol, theme；可选 rank。"""
    df = pd.read_csv(path, dtype={"symbol": str})
    return normalize_watchlist(df)


def load_theme_map_csv(path) -> pd.DataFrame:
    """题材成员。列：symbol, theme, start_date；可选 end_date。

    ``start_date`` 必填。空的开始日意味着「用现在的成分回填历史」，直接拒绝。
    ``end_date`` 空表示从 start_date 起仍在题材内，只向后有效，不向回填。
    """
    df = pd.read_csv(path, dtype={"symbol": str})
    return normalize_theme_map(df)


def normalize_watchlist(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in ("trade_date", "symbol", "theme") if c not in df.columns]
    if missing:
        raise ValueError(f"晨报 CSV 缺列 {missing}，需要 trade_date, symbol, theme")
    out = df.copy()
    out["symbol"] = _require_symbols(out["symbol"], "晨报 CSV")
    out["theme"] = out["theme"].astype(str).str.strip()
    if out["theme"].isin(["", "nan", "None"]).any() or out["theme"].isna().any():
        raise ValueError("晨报 CSV 有空的题材标签")
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.normalize()
    if out["trade_date"].isna().any():
        raise ValueError("晨报 CSV 有无法解析的 trade_date")
    if "rank" not in out.columns:
        out["rank"] = pd.NA
    else:
        out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    dup = out.duplicated(["trade_date", "symbol", "theme"], keep=False)
    if dup.any():
        raise ValueError("晨报 CSV 存在重复的 (trade_date, symbol, theme)，无法决定保留哪一行")
    if out.empty:
        raise ValueError("晨报 CSV 是空的")
    return out.loc[:, ["trade_date", "symbol", "theme", "rank"]].reset_index(drop=True)


def normalize_theme_map(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in ("symbol", "theme", "start_date") if c not in df.columns]
    if missing:
        raise ValueError(
            f"题材成员表缺列 {missing}，需要 symbol, theme, start_date"
            "（end_date 可选）。缺 start_date 不会把当前成分回填到历史。")
    out = df.copy()
    out["symbol"] = _require_symbols(out["symbol"], "题材成员表")
    out["theme"] = out["theme"].astype(str).str.strip()
    if out["theme"].isin(["", "nan", "None"]).any() or out["theme"].isna().any():
        raise ValueError("题材成员表有空的题材标签")
    out["start_date"] = pd.to_datetime(out["start_date"], errors="coerce").dt.normalize()
    if out["start_date"].isna().any():
        raise ValueError(
            "题材成员表有空的或无法解析的 start_date。"
            "不能把「现在的成分」当成历史上一直都在。")
    if "end_date" in out.columns:
        out["end_date"] = pd.to_datetime(out["end_date"], errors="coerce").dt.normalize()
    else:
        out["end_date"] = pd.NaT
    bad = out["end_date"].notna() & (out["end_date"] < out["start_date"])
    if bad.any():
        raise ValueError("题材成员表有 end_date 早于 start_date 的行")
    if out.empty:
        raise ValueError("题材成员表是空的")
    return out.loc[:, ["symbol", "theme", "start_date", "end_date"]].reset_index(drop=True)


def _norm_benchmark(series: pd.Series) -> pd.Series:
    idx = pd.DatetimeIndex(pd.to_datetime(series.index))
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    out = pd.Series(pd.to_numeric(series.to_numpy(), errors="coerce"), index=idx.normalize())
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")].dropna()
    out.name = getattr(series, "name", None) or "benchmark"
    if out.empty:
        raise ValueError("基准序列在去掉空值之后是空的")
    return out


class HypeWatchStrategy(Strategy):
    """观察池仿真策略。``generate_target`` 不做 I/O、不算成本、不下单。"""

    name = "hype_watch"
    paper_only = True

    def __init__(self, params: HypeWatchParams | None = None,
                 watchlist: pd.DataFrame | None = None,
                 theme_map: pd.DataFrame | None = None,
                 benchmark: pd.Series | None = None,
                 excluded_symbols: Sequence[str] | None = None) -> None:
        self.params = params or HypeWatchParams()
        if not self.params.paper_only:
            raise ValueError("hype_watch 拒绝 paper_only=false")
        self._watchlist = (normalize_watchlist(watchlist)
                           if watchlist is not None else None)
        self._theme_map = (normalize_theme_map(theme_map)
                           if theme_map is not None else None)
        self._mode = resolve_input_mode(self.params, self._watchlist)
        if self.params.excess_gate_active(self._mode) and benchmark is None:
            raise ValueError(
                "设置了 min_theme_excess，但没有基准价格序列。"
                "超额门槛不会静默关掉；请提供 benchmark.csv，或把 min_theme_excess 写成 null。")
        self._benchmark = _norm_benchmark(benchmark) if benchmark is not None else None
        self.excluded_symbols = frozenset(
            str(s) for s in (excluded_symbols if excluded_symbols is not None else ("000300",)))
        self.signal_log: list[dict] = []
        self.warnings: list[str] = []
        self._warned: set[str] = set()
        self._cooldown_left = 0
        self._circuit_flat = False
        self._circuit_reason = ""
        self._last_dd: float | None = None
        # 日收益需要前一交易日。断路的 20 日窗口不够时记 circuit_warmup，不把 warmup 设成 20
        # （否则短回测会告警「因子全空」，和「断路还没开始评估」不是一回事）。
        self.warmup = 2
        if self.params.input_mode == "auto" and self._watchlist is None:
            self._warn("auto_rules", "hype_watch: 没有晨报 CSV，使用纯规则路径。")
        if self._mode == "csv" and not self.params.csv_require_trigger:
            self._warn(
                "csv_replay",
                "hype_watch: csv_require_trigger=false，涨停家数与超额门槛不生效，只回放当日名单。")
        if self.params.rebalance_frequency != "D":
            self._warn(
                "freq",
                "hype_watch: 冷静期按信号日计数。调仓频率不是 D 时，"
                "cooldown_days 不等于那么多个交易日。")

    def _warn(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(message)

    def observe_equity(self, as_of, equity: pd.Series) -> None:
        """引擎在 ``generate_target`` 之前调用。只用 ``index <= as_of`` 的权益。"""
        ts = pd.Timestamp(as_of).normalize()
        self._circuit_flat = False
        self._circuit_reason = ""
        if self._cooldown_left > 0:
            self._circuit_flat = True
            self._circuit_reason = "circuit_cooldown"
            return
        if not self.params.circuit_enabled:
            self._circuit_reason = "circuit_off"
            return
        eq = self._equity_until(equity, ts)
        if len(eq) < int(self.params.circuit_lookback):
            self._circuit_reason = "circuit_warmup"
            return
        window = eq.iloc[-int(self.params.circuit_lookback):]
        peak = float(window.max())
        last = float(eq.iloc[-1])
        if not (peak > 0.0 and last > 0.0):
            self._circuit_reason = "circuit_bad_equity"
            return
        dd = last / peak - 1.0
        self._last_dd = dd
        # 1e-9 只吸收 92/100-1 这类二进制误差，挡不住 1bp 量级的「差一点」。
        if dd <= float(self.params.circuit_max_drawdown) + 1e-9:
            self._circuit_flat = True
            self._circuit_reason = "circuit_breaker"
            self._cooldown_left = int(self.params.circuit_cooldown_days)

    def _equity_until(self, equity: pd.Series, ts: pd.Timestamp) -> pd.Series:
        if equity is None or len(equity) == 0:
            return pd.Series(dtype="float64")
        idx = pd.DatetimeIndex(pd.to_datetime(equity.index))
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        idx = idx.normalize()
        if idx.has_duplicates:
            self._warn("eq_dup", "权益曲线有重复日期，断路取同一天最后一条。")
        eq = pd.Series(pd.to_numeric(pd.Series(equity).to_numpy(), errors="coerce"), index=idx)
        eq = eq.sort_index()
        eq = eq[~eq.index.duplicated(keep="last")]
        return eq.loc[:ts].dropna()

    def generate_target(self, as_of, bars: pd.DataFrame,
                        universe: Sequence[str]) -> TargetPortfolio:
        ts = pd.Timestamp(as_of).normalize()
        frame = self._cut_bars(bars, ts)
        allowed = self._allowed_universe(universe)
        weights, rows = self._build(ts, frame, allowed)
        if self._circuit_flat:
            if self._cooldown_left > 0:
                self._cooldown_left -= 1
            weights = {}
            for row in rows:
                if row["kind"] == "watch":
                    row["selected"] = False
                    row["weight"] = 0.0
                    row["reason"] = self._circuit_reason
                elif row["kind"] == "flat":
                    # 规则本身可能是「今天没触发」。断路仍然是空仓的原因，写在 flat 行上。
                    row["reason"] = self._circuit_reason
            if not any(r["kind"] == "flat" for r in rows):
                rows.append(self._blank(ts, "flat", self._circuit_reason))
        self.signal_log.extend(rows)
        return TargetPortfolio(as_of=ts, weights=weights)

    # ---- 输入整理 ----
    @staticmethod
    def _cut_bars(bars: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
        if bars is None or len(bars) == 0:
            return pd.DataFrame()
        out = bars.copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.normalize()
        out["symbol"] = out["symbol"].astype(str)
        out = out.loc[out["trade_date"] <= ts]
        if out["symbol"].duplicated().any() and out.duplicated(["trade_date", "symbol"]).any():
            dups = (out.loc[out.duplicated(["trade_date", "symbol"], keep=False), "symbol"]
                    .astype(str).unique().tolist())
            raise ValueError(f"bars 在同一交易日有重复 symbol：{dups[:5]}")
        return out

    def _allowed_universe(self, universe: Sequence[str]) -> list[str]:
        out: list[str] = []
        for raw in universe or []:
            sym = str(raw).strip()
            if not _SYMBOL_RE.fullmatch(sym):
                continue
            if sym in self.excluded_symbols:
                continue
            if not sym.startswith(MAIN_CHINEXT_PREFIXES):
                continue
            out.append(sym)
        return out

    def _blank(self, ts, kind: str, reason: str, **kw) -> dict:
        row = {
            "trade_date": ts,
            "kind": kind,
            "theme": kw.get("theme", ""),
            "symbol": kw.get("symbol", ""),
            "triggered": bool(kw.get("triggered", False)),
            "selected": bool(kw.get("selected", False)),
            "n_limitup": kw.get("n_limitup", pd.NA),
            "n_limit_unknown": kw.get("n_limit_unknown", pd.NA),
            "n_eligible": kw.get("n_eligible", pd.NA),
            "n_with_return": kw.get("n_with_return", pd.NA),
            "n_excluded_board": kw.get("n_excluded_board", pd.NA),
            "theme_return": kw.get("theme_return", pd.NA),
            "benchmark_return": kw.get("benchmark_return", pd.NA),
            "theme_excess": kw.get("theme_excess", pd.NA),
            "rank": kw.get("rank", pd.NA),
            "weight": float(kw.get("weight", 0.0) or 0.0),
            "reason": reason,
            "mode": "paper",
            "label": PAPER_LABEL,
            "execution_assumption": (
                f"T+1 {self.params.execution_price}（观察仿真，非委托）"),
            "circuit_drawdown": (self._last_dd if self._last_dd is not None else pd.NA),
        }
        return row

    def _build(self, ts, bars: pd.DataFrame, allowed: list[str]):
        if not allowed:
            # 宇宙为空，或里面没有主板/创业板代码（688、北交所、非 6 位代码都进这里）。
            return {}, [self._blank(ts, "flat", "no_eligible_universe")]
        if self._mode == "csv" and not self.params.csv_require_trigger:
            return self._csv_replay(ts, bars, allowed)
        self._require_limit_columns(bars)
        if self.params.excess_gate_active(self._mode):
            self._require_adj(bars)
        return self._rule_path(ts, bars, allowed)

    def _require_limit_columns(self, bars: pd.DataFrame) -> None:
        if bars is None or bars.empty or "limit_up" not in bars.columns or "close" not in bars.columns:
            raise ValueError(
                "涨停规则需要 bars.close 与 bars.limit_up。"
                "缺列不会用 pre_close*10% 推算（ST / 创业板 / 科创板规则不同）。")

    def _require_adj(self, bars: pd.DataFrame) -> None:
        if bars is None or bars.empty or "adj_close" not in bars.columns:
            raise ValueError(
                "题材超额需要 adj_close（后复权）。缺列不会改用原始收盘价，避免除权日假超额。")

    # ---- 规则 ----
    def _rule_path(self, ts, bars, allowed: list[str]):
        allowed_set = set(allowed)
        membership = self._membership(ts, bars, allowed_set)
        if not membership:
            return {}, [self._blank(ts, "flat", "no_theme")]
        today = self._day_rows(bars, ts)
        prev = self._prev_date(bars, ts)
        prev_rows = self._day_rows(bars, prev) if prev is not None else {}
        bench_ret = self._benchmark_return(ts)
        infos = []
        rows = []
        for theme in sorted(membership):
            info = self._eval_theme(
                ts, theme, membership[theme], allowed_set, today, prev_rows, prev, bench_ret)
            rows.append(info["log"])
            if info["triggered"]:
                infos.append(info)
        infos.sort(key=lambda x: (-x["n_limitup"], -x["excess_sort"], x["theme"]))
        infos = infos[: int(self.params.max_themes)]
        if self._mode == "csv":
            weights, watch_rows = self._gate_csv(ts, bars, allowed_set, infos)
        else:
            weights, watch_rows = self._pool_from_rules(ts, infos, today)
        if not watch_rows and not weights:
            rows.append(self._blank(ts, "flat", "no_trigger"))
        return weights, rows + watch_rows

    def _membership(self, ts, bars, allowed_set: set[str]) -> dict[str, set[str]]:
        if self._theme_map is not None:
            return self._members_from_map(ts)
        today = bars.loc[bars["trade_date"] == ts] if not bars.empty else bars
        col = None
        if not today.empty and "theme" in today.columns and today["theme"].notna().any():
            col = "theme"
        elif not today.empty and "industry" in today.columns and today["industry"].notna().any():
            col = "industry"
            self._warn(
                "industry_proxy",
                "hype_watch: 用 bars.industry 当作题材代理。若该列是当前静态分类，"
                "存在前视（README 限制第 5、30 条）。有逐日题材请改用 themes.csv。")
        if col is None:
            raise ValueError(
                "纯规则路径没有题材成员：请提供 themes.csv（含 start_date），"
                "或在信号日的 bars 上给出 theme / industry 列。"
                "不会把全市场当成一条题材，也不会读取 instruments 里的静态行业。")
        out: dict[str, set[str]] = {}
        for sym, theme in zip(today["symbol"].astype(str), today[col]):
            if pd.isna(theme):
                continue
            name = str(theme).strip()
            if not name or name in {"nan", "None"}:
                continue
            out.setdefault(name, set()).add(str(sym))
        return out

    def _members_from_map(self, ts: pd.Timestamp) -> dict[str, set[str]]:
        m = self._theme_map
        assert m is not None
        active = m.loc[m["start_date"] <= ts]
        end = active["end_date"]
        active = active.loc[end.isna() | (end >= ts)]
        out: dict[str, set[str]] = {}
        for sym, theme in zip(active["symbol"].astype(str), active["theme"].astype(str)):
            out.setdefault(theme, set()).add(sym)
        return out

    @staticmethod
    def _day_rows(bars: pd.DataFrame, ts) -> dict[str, pd.Series]:
        if bars is None or bars.empty or ts is None or pd.isna(ts):
            return {}
        day = bars.loc[bars["trade_date"] == pd.Timestamp(ts)]
        if day.empty:
            return {}
        indexed = day.set_index("symbol", drop=False)
        return {str(sym): row for sym, row in indexed.iterrows()}

    @staticmethod
    def _prev_date(bars: pd.DataFrame, ts: pd.Timestamp):
        if bars is None or bars.empty:
            return None
        prev = bars.loc[bars["trade_date"] < ts, "trade_date"]
        if prev.empty:
            return None
        return pd.Timestamp(prev.max())

    def _benchmark_return(self, ts: pd.Timestamp) -> float | None:
        if self._benchmark is None:
            return None
        s = self._benchmark
        if ts not in s.index:
            return None
        prev_idx = s.index[s.index < ts]
        if len(prev_idx) == 0:
            return None
        prev = float(s.loc[prev_idx[-1]])
        cur = float(s.loc[ts])
        if prev <= 0.0 or cur <= 0.0:
            return None
        return cur / prev - 1.0

    def _limit_status(self, row: pd.Series) -> str:
        if _as_bool(row.get("limit_exempt", False)):
            return "exempt"
        lu = row.get("limit_up", None)
        close = row.get("close", None)
        if lu is None or close is None or pd.isna(lu) or pd.isna(close):
            return "unknown"
        if float(lu) <= 0.0:
            return "unknown"
        if float(close) + float(self.params.limitup_abs_tol) >= float(lu):
            return "up"
        return "no"

    def _member_return(self, sym: str, today: Mapping[str, pd.Series],
                       prev_rows: Mapping[str, pd.Series]) -> float | None:
        row = today.get(sym)
        prev = prev_rows.get(sym)
        if row is None or prev is None:
            return None
        a1 = row.get("adj_close", None)
        a0 = prev.get("adj_close", None)
        if a1 is None or a0 is None or pd.isna(a1) or pd.isna(a0):
            return None
        a1f, a0f = float(a1), float(a0)
        if a1f <= 0.0 or a0f <= 0.0:
            return None
        return a1f / a0f - 1.0

    def _eval_theme(self, ts, theme, members: set[str], allowed_set: set[str],
                    today, prev_rows, prev, bench_ret):
        n_excluded_board = 0
        eligible: list[str] = []
        for sym in sorted(members):
            on_board = (_SYMBOL_RE.fullmatch(sym)
                        and sym.startswith(MAIN_CHINEXT_PREFIXES)
                        and sym not in self.excluded_symbols)
            if not on_board:
                n_excluded_board += 1
                continue
            if sym not in allowed_set:
                continue
            row = today.get(sym)
            if row is None:
                continue
            if _as_bool(row.get("is_st", False)) or _as_bool(row.get("is_suspended", False)):
                continue
            eligible.append(sym)
        n_up = 0
        n_unknown = 0
        for sym in eligible:
            status = self._limit_status(today[sym])
            if status == "up":
                n_up += 1
            elif status == "unknown":
                n_unknown += 1
        rets = []
        if prev is not None:
            for sym in eligible:
                r = self._member_return(sym, today, prev_rows)
                if r is not None:
                    rets.append(r)
        theme_ret = float(sum(rets) / len(rets)) if rets else None
        excess = None
        if theme_ret is not None and bench_ret is not None:
            excess = theme_ret - bench_ret
        reason = "ok"
        triggered = False
        if n_unknown > 0:
            reason = "missing_limit"
        elif prev is None:
            reason = "no_prev_bar"
        elif len(rets) < int(self.params.min_theme_members):
            reason = "insufficient_members"
        elif n_up < int(self.params.min_limitup_count):
            reason = "limitup_below_threshold"
        elif self.params.excess_gate_active(self._mode):
            if bench_ret is None:
                reason = "benchmark_gap"
            elif theme_ret is None:
                reason = "no_theme_return"
            elif excess < float(self.params.min_theme_excess):
                reason = "excess_below_threshold"
            else:
                triggered = True
                reason = "triggered"
        else:
            triggered = True
            reason = "triggered"
        if not eligible and reason == "ok":
            reason = "no_eligible_members"
        excess_sort = float(excess) if excess is not None else -1e18
        log = self._blank(
            ts, "theme", reason, theme=theme, triggered=triggered,
            n_limitup=n_up, n_limit_unknown=n_unknown, n_eligible=len(eligible),
            n_with_return=len(rets), n_excluded_board=n_excluded_board,
            theme_return=(theme_ret if theme_ret is not None else pd.NA),
            benchmark_return=(bench_ret if bench_ret is not None else pd.NA),
            theme_excess=(excess if excess is not None else pd.NA))
        return {
            "theme": theme,
            "triggered": triggered,
            "n_limitup": n_up,
            "excess_sort": excess_sort,
            "eligible": eligible,
            "log": log,
        }

    def _assign_weights(self, symbols: list[str]) -> tuple[dict[str, float], str]:
        n = len(symbols)
        if n == 0:
            return {}, ""
        if self.params.position_mode == "equal":
            w = float(self.params.gross_exposure) / n
            return {s: w for s in symbols}, "equal_weight"
        w = float(self.params.fixed_weight)
        if n * w > float(self.params.gross_exposure) + 1e-12:
            w = float(self.params.gross_exposure) / n
            return {s: w for s in symbols}, "fixed_scaled_to_gross"
        return {s: w for s in symbols}, "fixed_weight"

    def _round_robin(self, theme_order: list[str],
                     queues: dict[str, list[str]]) -> list[str]:
        picked: list[str] = []
        seen: set[str] = set()
        pos = {t: 0 for t in theme_order}
        progressed = True
        while len(picked) < int(self.params.pool_size) and progressed:
            progressed = False
            for theme in theme_order:
                q = queues.get(theme, [])
                i = pos[theme]
                while i < len(q) and q[i] in seen:
                    i += 1
                pos[theme] = i
                if i >= len(q):
                    continue
                seen.add(q[i])
                picked.append(q[i])
                pos[theme] = i + 1
                progressed = True
                if len(picked) >= int(self.params.pool_size):
                    break
        return picked

    def _pool_from_rules(self, ts, infos, today):
        theme_order = [info["theme"] for info in infos]
        queues: dict[str, list[str]] = {}
        for info in infos:
            ranked = []
            for sym in info["eligible"]:
                row = today.get(sym)
                if row is None or self._limit_status(row) != "up":
                    continue
                amt = row.get("amount", None)
                amt_v = float(amt) if amt is not None and not pd.isna(amt) else -1.0
                ranked.append((-amt_v, sym))
            ranked.sort()
            queues[info["theme"]] = [sym for _, sym in ranked]
        picked = self._round_robin(theme_order, queues)
        weights, why = self._assign_weights(picked)
        theme_of = {}
        for theme, q in queues.items():
            for sym in q:
                theme_of.setdefault(sym, theme)
        rows = []
        picked_set = set(picked)
        for i, sym in enumerate(picked, start=1):
            rows.append(self._blank(
                ts, "watch", f"triggered:{why}", theme=theme_of.get(sym, ""),
                symbol=sym, triggered=True, selected=True, rank=i,
                weight=weights[sym]))
        for theme, q in queues.items():
            for sym in q:
                if sym in picked_set:
                    continue
                rows.append(self._blank(
                    ts, "watch", "pool_full", theme=theme, symbol=sym,
                    triggered=True, selected=False))
        return weights, rows

    def _watch_today(self, ts: pd.Timestamp) -> list[dict]:
        if self._watchlist is None:
            return []
        day = self._watchlist.loc[self._watchlist["trade_date"] == ts]
        out = []
        for rec in day.itertuples(index=False):
            rank = rec.rank
            if rank is not None and not pd.isna(rank):
                rank_key = (0, float(rank))
            else:
                rank_key = (1, 1e18)
            out.append({
                "symbol": str(rec.symbol),
                "theme": str(rec.theme),
                "rank_key": rank_key,
            })
        return out

    def _gate_csv(self, ts, bars, allowed_set: set[str], infos):
        today_list = self._watch_today(ts)
        if not today_list:
            return {}, [self._blank(ts, "flat", "no_watchlist_row")]
        triggered = {info["theme"] for info in infos}
        theme_order = [info["theme"] for info in infos]
        row_map = self._day_rows(bars, ts)
        queues: dict[str, list[str]] = {t: [] for t in theme_order}
        pending: dict[str, list[tuple]] = {t: [] for t in theme_order}
        rows = []
        for rec in sorted(today_list, key=lambda r: (r["rank_key"], r["symbol"])):
            sym, theme = rec["symbol"], rec["theme"]
            if sym not in allowed_set:
                rows.append(self._blank(
                    ts, "watch", "not_in_universe_or_board", theme=theme, symbol=sym))
                continue
            row = row_map.get(sym)
            if row is None:
                rows.append(self._blank(ts, "watch", "no_bar", theme=theme, symbol=sym))
                continue
            if _as_bool(row.get("is_st", False)):
                rows.append(self._blank(ts, "watch", "st", theme=theme, symbol=sym))
                continue
            if _as_bool(row.get("is_suspended", False)):
                rows.append(self._blank(ts, "watch", "suspended", theme=theme, symbol=sym))
                continue
            if theme not in triggered:
                rows.append(self._blank(
                    ts, "watch", "theme_not_triggered", theme=theme, symbol=sym))
                continue
            pending[theme].append((rec["rank_key"], sym))
        for theme, items in pending.items():
            items.sort()
            queues[theme] = [sym for _, sym in items]
        picked = self._round_robin(theme_order, queues)
        weights, why = self._assign_weights(picked)
        theme_of = {}
        for theme, q in queues.items():
            for sym in q:
                theme_of.setdefault(sym, theme)
        picked_set = set(picked)
        logged: set[str] = set()
        for i, sym in enumerate(picked, start=1):
            rows.append(self._blank(
                ts, "watch", f"csv_triggered:{why}", theme=theme_of.get(sym, ""),
                symbol=sym, triggered=True, selected=True, rank=i, weight=weights[sym]))
            logged.add(sym)
        for theme, q in queues.items():
            for sym in q:
                if sym in picked_set or sym in logged:
                    continue
                rows.append(self._blank(
                    ts, "watch", "pool_full", theme=theme, symbol=sym, triggered=True))
                logged.add(sym)
        return weights, rows

    def _csv_replay(self, ts, bars, allowed: list[str]):
        today_list = self._watch_today(ts)
        if not today_list:
            return {}, [self._blank(ts, "flat", "no_watchlist_row")]
        allowed_set = set(allowed)
        row_map = self._day_rows(bars, ts)
        by_theme: dict[str, list[tuple]] = {}
        rows = []
        for rec in today_list:
            sym, theme = rec["symbol"], rec["theme"]
            if sym not in allowed_set:
                rows.append(self._blank(
                    ts, "watch", "not_in_universe_or_board", theme=theme, symbol=sym))
                continue
            row = row_map.get(sym)
            if row is None:
                rows.append(self._blank(ts, "watch", "no_bar", theme=theme, symbol=sym))
                continue
            if _as_bool(row.get("is_st", False)):
                rows.append(self._blank(ts, "watch", "st", theme=theme, symbol=sym))
                continue
            if _as_bool(row.get("is_suspended", False)):
                rows.append(self._blank(ts, "watch", "suspended", theme=theme, symbol=sym))
                continue
            by_theme.setdefault(theme, []).append((rec["rank_key"], sym))
        # 名单只数多的题材在前；只数相同按题材名。这是回放顺序，不是收益排序。
        theme_order = sorted(by_theme, key=lambda t: (-len(by_theme[t]), t))
        theme_order = theme_order[: int(self.params.max_themes)]
        queues = {}
        for theme in theme_order:
            items = sorted(by_theme[theme])
            queues[theme] = [sym for _, sym in items]
        for theme in by_theme:
            if theme in queues:
                continue
            for _, sym in by_theme[theme]:
                rows.append(self._blank(
                    ts, "watch", "theme_cap", theme=theme, symbol=sym))
        picked = self._round_robin(theme_order, queues)
        weights, why = self._assign_weights(picked)
        theme_of = {}
        for theme, q in queues.items():
            for sym in q:
                theme_of.setdefault(sym, theme)
        picked_set = set(picked)
        for i, sym in enumerate(picked, start=1):
            rows.append(self._blank(
                ts, "watch", f"csv_replay:{why}", theme=theme_of.get(sym, ""),
                symbol=sym, selected=True, rank=i, weight=weights[sym]))
        for theme, q in queues.items():
            for sym in q:
                if sym in picked_set:
                    continue
                rows.append(self._blank(
                    ts, "watch", "pool_full", theme=theme, symbol=sym))
        if not picked:
            rows.append(self._blank(ts, "flat", "csv_replay_empty"))
        return weights, rows


def runtime_overrides(params: HypeWatchParams, *, freq: str | None = None,
                      execution_price: str | None = None,
                      max_positions: int | None = None,
                      industry_neutral: bool = False) -> dict:
    """回测脚本用的运行参数。持仓上限锁在 pool_size，避免等权被字典序截断。"""
    if industry_neutral:
        raise ValueError("hype_watch 不使用 --industry-neutral")
    if max_positions is not None:
        raise ValueError(
            "hype_watch 的持仓数由 hype_watch.pool_size 决定。"
            "不要用 --max-positions：权重相同的时候截断会按代码字典序丢掉名次。")
    resolved_freq = freq or params.rebalance_frequency
    resolved_px = execution_price or params.execution_price
    if resolved_px not in ("open", "close"):
        raise ValueError("execution_price 必须是 open 或 close")
    if resolved_freq not in ("D", "W", "M"):
        raise ValueError("rebalance_frequency 必须是 D、W 或 M")
    return {
        "rebalance_frequency": resolved_freq,
        "execution_price": resolved_px,
        "max_positions": int(params.pool_size),
        "max_weight_per_stock": 1.0,
    }


def build_observations(signal_log, bars: pd.DataFrame, horizons: Sequence[int],
                       calendar) -> pd.DataFrame:
    """事后观察：T0 收盘到 T0+h 收盘的后复权收益。

    只读已经记在信号日志里、``selected=True`` 的观察池标的。
    不参与目标权重。窗口超出本次日历记 ``pending``，缺行情记 ``missing``，
    不把缺失填成 0。
    """
    columns = ["signal_date", "symbol", "theme", "horizon", "observe_date",
               "ret_close", "status", "price_col", "label"]
    log = signal_log if isinstance(signal_log, pd.DataFrame) else pd.DataFrame(signal_log)
    empty = pd.DataFrame(columns=columns)
    if log.empty or "kind" not in log.columns:
        return empty
    picked = log.loc[(log["kind"] == "watch") & (log["selected"].map(_as_bool))]
    if picked.empty:
        return empty
    if bars is None or bars.empty or "adj_close" not in bars.columns:
        raise ValueError("观察窗口需要 bars.adj_close。缺列不会改用原始收盘价。")
    cal = pd.DatetimeIndex(pd.to_datetime(list(calendar))).normalize().sort_values().unique()
    px = bars.copy()
    px["trade_date"] = pd.to_datetime(px["trade_date"]).dt.normalize()
    px["symbol"] = px["symbol"].astype(str)
    if px.duplicated(["trade_date", "symbol"]).any():
        raise ValueError("观察窗口：bars 在同一交易日有重复 symbol")
    price = px.set_index(["trade_date", "symbol"])["adj_close"]
    out_rows = []
    for rec in picked.itertuples(index=False):
        sig = pd.Timestamp(rec.trade_date).normalize()
        sym = str(rec.symbol)
        theme = str(getattr(rec, "theme", "") or "")
        for h in horizons:
            h = int(h)
            base = {
                "signal_date": sig, "symbol": sym, "theme": theme, "horizon": h,
                "observe_date": pd.NaT, "ret_close": pd.NA, "status": "",
                "price_col": "adj_close", "label": PAPER_LABEL,
            }
            if sig not in cal:
                base["status"] = "not_in_calendar"
                out_rows.append(base)
                continue
            pos = int(cal.get_loc(sig))
            if pos + h >= len(cal):
                base["status"] = "pending"
                out_rows.append(base)
                continue
            obs = pd.Timestamp(cal[pos + h])
            base["observe_date"] = obs
            try:
                p0 = float(price.loc[(sig, sym)])
                p1 = float(price.loc[(obs, sym)])
            except KeyError:
                base["status"] = "missing"
                out_rows.append(base)
                continue
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0.0 or p1 <= 0.0:
                base["status"] = "missing"
                out_rows.append(base)
                continue
            base["ret_close"] = p1 / p0 - 1.0
            base["status"] = "observed"
            out_rows.append(base)
    return pd.DataFrame(out_rows, columns=columns)


def paper_summary(result, signal_log, params: HypeWatchParams) -> dict:
    """仿真摘要。回撤与 ``metrics.json`` 同一口径（暖机截断，README 限制第 18 条）。

    这里的净值与回撤只描述这一次纸面路径，不写成研究结论。
    """
    from ..metrics.performance import summarize

    summary = summarize(result)
    log = signal_log if isinstance(signal_log, pd.DataFrame) else pd.DataFrame(signal_log)
    if log.empty or "trade_date" not in log.columns:
        n_signal_days = 0
        n_selected = 0
    else:
        n_signal_days = int(pd.to_datetime(log["trade_date"]).nunique())
        selected = log.loc[(log["kind"] == "watch") & (log["selected"].map(_as_bool))]
        n_selected = int(len(selected))
    trades = result.trades
    if trades is None or trades.empty:
        n_rebalances = 0
    else:
        filled = trades.loc[(trades["shares"] > 0) & (trades["note"] == "filled")]
        n_rebalances = int(pd.to_datetime(filled["trade_date"]).nunique()) if len(filled) else 0
    return {
        "label": "paper",
        "disclaimer": PAPER_DISCLAIMER,
        "strategy": "hype_watch",
        "research_claim": "未验证。不得写入研究判定或 README 的收益表。",
        "final_equity": summary.get("final_equity"),
        "maximum_drawdown": summary.get("maximum_drawdown"),
        "maximum_drawdown_note": "与 metrics.json 同一口径（暖机截断，README 限制第 18 条）",
        "n_rebalances": n_rebalances,
        "n_rebalances_note": "至少有一笔 note=filled 且 shares>0 的成交日个数；受阻单不算",
        "total_costs": summary.get("total_costs"),
        "n_signal_days": n_signal_days,
        "n_selected_rows": n_selected,
        "one_way_friction": params.one_way_friction(),
        "one_way_friction_note": "佣金率+滑点率，不含 5 元最低佣金，不含卖出印花税",
        "stamp_duty_rate": params.stamp_duty_rate,
        "commission_min": params.commission_min,
        "execution_assumption": f"T 日收盘出信号，T+1 以 {params.execution_price} 撮合",
        "circuit_breaker": {
            "enabled": params.circuit_enabled,
            "lookback": params.circuit_lookback,
            "max_drawdown": params.circuit_max_drawdown,
            "cooldown_days": params.circuit_cooldown_days,
            "cooldown_note": "按信号日计数，含触发当日；日频时才是 N 个交易日",
        },
        "position_mode": params.position_mode,
        "gross_exposure": params.gross_exposure,
        "pool_size": params.pool_size,
        "max_themes": params.max_themes,
        "min_limitup_count": params.min_limitup_count,
        "min_theme_excess": params.min_theme_excess,
    }
