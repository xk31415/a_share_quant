"""回测引擎。

流程（每个交易日 d）：
    数据 -> 股票池 -> 因子/策略 -> 信号 -> 目标组合 -> 交易约束 -> 订单
    -> 成交模拟 -> 交易成本 -> 持仓 -> 净值 -> 绩效指标

时间对齐约定（防未来函数的核心）：
    * d 为信号日：策略只看到 trade_date <= d 的行情；
    * 目标权重于 d 收盘生成，d+1（下一交易日）开盘撮合；
    * 每个阶段（股票池/因子/策略/执行/成本/估值）都可单独测试。

数据校验约定（重要）：
    * ``run()`` 在启动阶段自行执行 ``validate_dataset``，**不依赖调用方脚本**。
      QUICKSTART 里的编程式用法同样会走数据校验与幸存者偏差哨兵；
    * 非致命问题收集到 ``BacktestResult.validation_warnings``；
    * 致命问题直接抛 ``DataValidationError``，不进入回测。

状态数据约定（重要）：
    * 停牌/ST 以 status 表为权威来源，启动时合并进 bars（status 优先，
      bars 同名列只在该 (date, symbol) 无 status 记录时兜底）；
    * ``enforce_limit=True`` 但 bars 缺 ``limit_up/limit_down`` 时启动即报错，
      不允许"配置写了开启、实际静默不生效"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from ..data.loader import DataProvider
from ..data.schema import coerce_bool
from ..data.validation import DataValidationError, validate_dataset
from ..portfolio.construction import PortfolioRules, build_target_weights
from ..strategies.base import Strategy
from ..universe.builder import UniverseBuilder
from ..utils.dates import RebalanceFrequency, rebalance_dates
from .costs import CostModel
from .execution import ExecutionSimulator, Order, generate_orders
from .positions import PortfolioState


@dataclass
class BacktestConfig:
    """回测配置（由 config/settings.yaml 提供，禁止硬编码进策略）。"""

    start_date: str
    end_date: str
    rebalance_frequency: RebalanceFrequency = "M"
    initial_capital: float = 1_000_000.0
    execution_price: str = "open"   # t+1 成交参考价列名
    benchmark: Optional[str] = None
    cash_buffer: float = 0.01       # 建仓时预留的现金比例（覆盖费用/滑点）

    def __post_init__(self) -> None:
        if not 0.0 <= self.cash_buffer < 1.0:
            raise ValueError("cash_buffer must be in [0, 1)")


@dataclass
class BacktestResult:
    """回测产出。"""

    equity_curve: pd.Series       # 日期 -> 总权益（收盘估值）
    daily_returns: pd.Series      # 日收益率
    positions: pd.DataFrame       # 每日持仓快照（含权重）
    trades: pd.DataFrame          # 逐笔成交（含受阻记录）
    turnover: pd.Series           # 调仓日单边换手率（(买+卖)/2/权益）
    costs: pd.Series              # 调仓日总成本（元）
    benchmark: Optional[pd.Series] = None
    config: Optional[BacktestConfig] = None
    buy_turnover: Optional[pd.Series] = None    # 买入成交额/权益
    sell_turnover: Optional[pd.Series] = None   # 卖出成交额/权益
    validation_warnings: list[str] = field(default_factory=list)
    universe_violations: Optional[pd.DataFrame] = None  # 策略给出的股票池外标的

    @property
    def final_equity(self) -> float:
        return float(self.equity_curve.iloc[-1])


class BacktestEngine:
    """事件驱动回测引擎。"""

    def __init__(self, provider: DataProvider, strategy: Strategy,
                 universe_builder: UniverseBuilder,
                 portfolio_rules: PortfolioRules, cost_model: CostModel,
                 config: BacktestConfig, lot_size: int = 100,
                 enforce_limit: bool = True,
                 enforce_suspension: bool = True,
                 validate: bool = True,
                 max_participation_rate: float = 0.0) -> None:
        self.provider = provider
        self.strategy = strategy
        self.universe_builder = universe_builder
        self.portfolio_rules = portfolio_rules
        self.cost_model = cost_model
        self.config = config
        self.lot_size = lot_size
        self.enforce_limit = enforce_limit
        self.enforce_suspension = enforce_suspension
        self.validate = validate
        self.max_participation_rate = max_participation_rate

    # ---- 启动阶段：数据准备与校验 ----
    def _prepare_data(self, start, end):
        """加载行情 + 状态，合并，并做数据校验。返回 (bars, warnings)。"""
        warnings: list[str] = []
        bars = self.provider.get_bars(start, end)
        if bars.empty:
            raise ValueError("no bars in backtest window")

        status = self._load_status(start, end)
        bars = self._attach_status(bars, status)

        if self.validate:
            calendar = self.provider.get_calendar()
            instruments = self.provider.get_instruments()
            warnings.extend(validate_dataset(
                bars=bars,
                instruments=instruments,
                status=status if status is not None and not status.empty else None,
                calendar=calendar))
            self._require_limit_columns(bars)
            self._require_volume_for_cap(bars)
        return bars, warnings

    def _require_volume_for_cap(self, bars: pd.DataFrame) -> None:
        """启用成交量上限时，volume 不允许有缺失。

        ``volume`` 是必填列，NaN 属于数据异常。若放行，执行层只能把该笔
        当成"没有上限"处理（历史缺陷：99,000 股照买、``capped=False``），
        容量约束就这么静默消失了。
        """
        if self.max_participation_rate <= 0:
            return
        n_na = int(bars["volume"].isna().sum())
        if n_na:
            raise DataValidationError(
                f"max_participation_rate={self.max_participation_rate} 已启用成交量上限，"
                f"但 bars.volume 有 {n_na} 行为空值，无法判断单笔可成交量。"
                "请补齐成交量，或把 market_rules.max_participation_rate 设为 0 关闭该约束。")

    def _load_status(self, start, end) -> Optional[pd.DataFrame]:
        try:
            status = self.provider.get_status(start, end)
        except FileNotFoundError:
            return None
        return status

    @staticmethod
    def _attach_status(bars: pd.DataFrame,
                       status: Optional[pd.DataFrame]) -> pd.DataFrame:
        """把 status 表的 is_st / is_suspended 合并进 bars。

        status 是权威来源（schema 里的必填表）；bars 上的同名列只在
        status 没有该 (trade_date, symbol) 记录时兜底——避免数据源只填了
        status 就被静默忽略（曾经的缺陷），也避免 bars 的旧标记覆盖新状态。
        """
        out = bars.copy()
        if status is None or status.empty:
            for col in ("is_st", "is_suspended"):
                if col in out.columns:
                    out[col] = out[col].fillna(False).astype(bool)
            return out

        st = status.loc[:, ["trade_date", "symbol", "is_st", "is_suspended"]].copy()
        st["trade_date"] = pd.to_datetime(st["trade_date"])
        out["trade_date"] = pd.to_datetime(out["trade_date"])
        merged = out.merge(st, on=["trade_date", "symbol"], how="left",
                           suffixes=("", "__status"))
        for col in ("is_st", "is_suspended"):
            status_col = f"{col}__status"
            if status_col in merged.columns:      # bars 也有同名列 -> 被加了后缀
                if col in out.columns:
                    merged[col] = merged[status_col].where(
                        merged[status_col].notna(), merged[col])
                else:  # pragma: no cover - 两列都缺时无同名列
                    merged[col] = merged[status_col]
                merged = merged.drop(columns=[status_col])
            merged[col] = coerce_bool(merged[col])
        return merged.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    def _require_limit_columns(self, bars: pd.DataFrame) -> None:
        """enforce_limit 打开时，涨跌停价的**逐行**缺失必须显式暴露。

        只检查"整列是否全空"是不够的：单行 NaN 会让 execution 里的
        ``pd.notna(limit_up)`` 为假，从而静默跳过该笔约束（历史缺陷）。
        无涨跌幅的合法场景（注册制新股上市前 5 日）请用 ``limit_exempt`` 显式标注，
        不要靠留空来表达。
        """
        if not self.enforce_limit:
            return
        missing = [c for c in ("limit_up", "limit_down") if c not in bars.columns]
        if missing:
            raise DataValidationError(
                f"enforce_limit=True 但 bars 缺少 {missing}。"
                "涨跌停约束无法执行——请提供 limit_up/limit_down 列，"
                "或在配置中显式设置 market_rules.consider_limit_up_down=false。"
                "不要用 pre_close*10% 自行推算：ST/创业板/科创板/北交所规则不同。")
        exempt = bars["limit_exempt"].astype(bool) if "limit_exempt" in bars.columns else None
        for col in ("limit_up", "limit_down"):
            na = bars[col].isna()
            if exempt is not None:
                na &= ~exempt
            n_na = int(na.sum())
            if n_na:
                raise DataValidationError(
                    f"enforce_limit=True 但 bars.{col} 有 {n_na} 行为空值"
                    f"（例如 {bars.loc[na, 'symbol'].head(3).tolist()}）。"
                    "留空会被当成「没有涨跌停」而静默放行。若无涨跌幅是规则本身"
                    "（如注册制新股上市前 5 日），请把 limit_exempt 标为 true；"
                    "否则按数据缺失处理并补齐。")

    def _attach_adj_factor(self, bars: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
        """按 (symbol) 生成复权因子列 ``adj_factor = adj_close / close``。

        估值时用 ``k_t / adj_ref`` 抵消除权除息的价格跳空；缺 ``adj_close`` 时
        全部记为 1（即按原始价估值），并**显式告警**——否则真实数据回测的净值
        会在每个除权日凭空腰斩，且看起来像正常下跌。
        """
        out = bars.copy()
        warnings: list[str] = []
        if "adj_close" not in out.columns:
            out["adj_factor"] = 1.0
            warnings.append(
                "公司行动：bars 无 adj_close 列，净值按原始价估值。"
                "10 送 10 等除权日中持仓市值会凭空腰斩，除权日收益不可信。"
                "接入真实数据时请提供后复权收盘价。")
            return out, warnings
        close = pd.to_numeric(out["close"], errors="coerce")
        adj = pd.to_numeric(out["adj_close"], errors="coerce")
        k = (adj / close).where((close > 0) & (adj > 0))
        # 单日缺失沿用前值，避免因子断点造成估值跳变
        out["adj_factor"] = (
            k.groupby(out["symbol"]).ffill().bfill().fillna(1.0).astype(float))
        return out, warnings

    # ---- 主循环 ----
    def run(self) -> BacktestResult:
        cfg = self.config
        cal = self.provider.get_calendar()
        trading_days = cal.slice(cfg.start_date, cfg.end_date)
        if len(trading_days) < 2:
            raise ValueError("backtest window must span at least 2 trading days")

        bars, warnings = self._prepare_data(trading_days[0], trading_days[-1])
        bars = bars.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        bars, adj_warnings = self._attach_adj_factor(bars)
        warnings.extend(adj_warnings)
        bars_by_date = {ts: g for ts, g in bars.groupby("trade_date")}
        signal_dates = set(rebalance_dates(trading_days, cfg.rebalance_frequency))
        warnings.extend(self._check_warmup(trading_days, signal_dates))

        state = PortfolioState(cfg.initial_capital)
        simulator = ExecutionSimulator(
            self.cost_model, lot_size=self.lot_size,
            enforce_limit=self.enforce_limit,
            enforce_suspension=self.enforce_suspension,
            max_participation_rate=self.max_participation_rate)

        last_prices: dict[str, float] = {}
        last_k: dict[str, float] = {}
        pending_weights: Optional[dict[str, float]] = None
        # 未成交的卖单（股数），次日继续挂；这是 F-02 的修复：
        # 卖单被跌停/停牌拦下后不能就这么丢了，否则持仓会一直超配到下一个信号日。
        pending_sells: dict[str, int] = {}
        # 退市日：到期仍未清仓的持仓按最后已知价强制清算（F-02 的第二个问题）
        instruments = self.provider.get_instruments()
        delist_dates = {}
        if "delist_date" in instruments.columns:
            for sym, dd in zip(instruments["symbol"], instruments["delist_date"]):
                if pd.notna(dd):
                    delist_dates[str(sym)] = pd.Timestamp(dd).normalize()
        prev_equity = cfg.initial_capital

        equity_records: list[tuple[pd.Timestamp, float]] = []
        position_rows: list[dict] = []
        trade_rows: list[dict] = []
        violation_rows: list[dict] = []
        turnover_records: dict[pd.Timestamp, float] = {}
        buy_turnover_records: dict[pd.Timestamp, float] = {}
        sell_turnover_records: dict[pd.Timestamp, float] = {}
        cost_records: dict[pd.Timestamp, float] = {}

        for d in trading_days:
            day_bars = bars_by_date.get(d, bars.iloc[0:0])

            # --- 先更新当日复权因子（停牌日不更新，与价格同口径） ---
            if not day_bars.empty:
                k_mask = day_bars["adj_factor"].notna()
                if "is_suspended" in day_bars.columns:
                    k_mask &= ~day_bars["is_suspended"].astype(bool)
                for sym, kv in zip(day_bars.loc[k_mask, "symbol"],
                                   day_bars.loc[k_mask, "adj_factor"]):
                    last_k[str(sym)] = float(kv)

            # --- 开盘前：T+1 释放昨日及更早持有的份额 ---
            state.start_of_day()

            # --- 退市清算：已到退市日的持仓一律清掉 ---
            # 不判"当天有没有行情"：退市了就是不能再持有。当天还有行情的票，
            # 用最后已知价结算是保守选择（真实世界退市整理期价格通常更低）。
            # 清掉之后当天**不许再买回**，否则会「强平 -> 同一信号买回 -> 次日再强平」
            # 空转（R08 F-42：原注释写"当天又无行情"，但代码只判了日期）。
            forced_today: set[str] = set()
            for sym in list(state.positions):
                dd = delist_dates.get(sym)
                if dd is None or d < dd:
                    continue
                pos = state.positions.get(sym)
                px = last_prices.get(sym)
                if pos is None or px is None:
                    continue          # 从没估过价的（极罕见），留给估值环节报错
                # 记强平**前**的股数：sell() 会把 pos.shares 减到 0，
                # 之后再读就是 0（R08 F-41）。
                forced_shares = pos.shares
                state.sell(sym, forced_shares, px, 0.0, last_k.get(sym, 1.0))
                trade_rows.append({
                    "trade_date": d, "symbol": sym, "direction": "sell",
                    "shares": forced_shares, "price": px, "cost": 0.0,
                    "note": "forced:delisted", "capped": False})
                pending_sells.pop(sym, None)
                forced_today.add(sym)

            # --- 开盘：执行上一信号日生成的目标权重（当日开盘价撮合） ---
            if (pending_weights is not None or pending_sells) and not day_bars.empty:
                ref_prices = (
                    day_bars.set_index("symbol")[cfg.execution_price]
                    .astype(float).to_dict()
                )
                # 定仓基数要扣掉"当天无法交易"的持仓市值（停牌/退市无行情）。
                # 否则这部分冻结市值会被当成可用资金重复分配，
                # 导致每次调仓都现金不足、大量买单被缩减。
                tradeable = self._tradeable_symbols(day_bars, cfg.execution_price)
                sizing_equity = self._sizing_equity(state, prev_equity,
                                                    tradeable, last_prices, last_k)
                orders = []
                target = pending_weights or {}
                if forced_today:
                    # 今天刚被退市强平的票，不能再进目标（R08 F-42）
                    target = {k: v for k, v in target.items()
                              if k not in forced_today}
                if pending_weights is not None:
                    orders = generate_orders(target, state, sizing_equity,
                                             ref_prices, self.lot_size,
                                             cash_buffer=cfg.cash_buffer)
                # 补挂上一日没卖掉的（目标里已没有的标的）
                for sym, sh in list(pending_sells.items()):
                    if sym in target:
                        pending_sells.pop(sym, None)      # 新信号接管
                    elif sym in ref_prices and sh > 0:
                        # 注意：不能设 reason。Order.reason 的语义是"本来就下不出去"，
                        # 设了会被执行层当成拒绝单直接跳过（踩过一次）。
                        orders.append(Order(sym, "sell", int(sh)))
                fills = simulator.execute(orders, day_bars, state, last_k)

                # 卖单没成交（跌停/停牌/无量/部分成交）就留到下一日继续挂
                for sym in {o.symbol for o in orders if o.direction == "sell"}:
                    pos = state.positions.get(sym)
                    if pos is not None and pos.shares > 0 and sym not in target:
                        pending_sells[sym] = pos.shares
                    else:
                        pending_sells.pop(sym, None)
                day_buy = 0.0
                day_sell = 0.0
                day_cost = 0.0
                for f in fills:
                    trade_rows.append({
                        "trade_date": d, "symbol": f.symbol,
                        "direction": f.direction, "shares": f.shares,
                        "price": f.price, "cost": f.cost, "note": f.note,
                        "capped": bool(f.capped)})
                    if f.direction == "buy":
                        day_buy += f.shares * f.price
                    else:
                        day_sell += f.shares * f.price
                    day_cost += f.cost
                if fills and (day_buy > 0 or day_sell > 0 or day_cost > 0):
                    denom = max(prev_equity, 1e-12)
                    buy_turnover_records[d] = day_buy / denom
                    sell_turnover_records[d] = day_sell / denom
                    # 单边换手率：买卖对称时约等于一侧成交额/权益
                    turnover_records[d] = (day_buy + day_sell) / 2.0 / denom
                    cost_records[d] = day_cost
                pending_weights = None

            # --- 收盘估值（停牌日不用当日价，缺失则沿用最近价） ---
            # 停牌日数据商可能仍给一根"理论价" bar；直接拿来估值会让净值凭空跳变
            # （实测过停牌日 OHLC 翻倍 -> 权益一天 +100%）。交易被拦住了，涨跌没被拦住。
            if not day_bars.empty:
                px_mask = day_bars["close"].notna()
                if "is_suspended" in day_bars.columns:
                    px_mask &= ~day_bars["is_suspended"].astype(bool)
                for sym, px in zip(day_bars.loc[px_mask, "symbol"],
                                   day_bars.loc[px_mask, "close"]):
                    last_prices[str(sym)] = float(px)
            equity = state.market_value(last_prices, last_k)
            equity_records.append((d, equity))
            for sym, pos in sorted(state.positions.items()):
                px = last_prices.get(sym, pos.avg_cost)
                value = state.position_value(sym, px, last_k.get(sym, 1.0))
                position_rows.append({
                    "trade_date": d, "symbol": sym, "shares": pos.shares,
                    "available_shares": pos.available_shares,
                    "avg_cost": round(pos.avg_cost, 6),
                    "weight": (value / equity) if equity > 0 else 0.0})

            # --- 信号日收盘：生成次日执行的目标权重 ---
            if d in signal_dates:
                hist = bars.loc[bars["trade_date"] <= d]
                universe = self.universe_builder.get_universe(d)
                warnings.extend(getattr(self.universe_builder, "warnings", []) or [])
                target = self.strategy.generate_target(d, hist, universe)
                weights, violations = self._enforce_universe(target.weights, universe)
                for sym, w in violations:
                    violation_rows.append(
                        {"trade_date": d, "symbol": sym, "weight": w})
                if violations:
                    warnings.append(
                        f"universe: {pd.Timestamp(d).date()} 策略给出 "
                        f"{len(violations)} 个股票池外的标的"
                        f"（例如 {[s for s, _ in violations][:5]}），已被引擎拦截，"
                        f"未下单。明细见 universe_violations.csv。")
                pending_weights = build_target_weights(weights,
                                                       self.portfolio_rules)
            prev_equity = equity

        warnings.extend(self.universe_builder.finalize_warnings())

        equity_curve = pd.Series(dict(equity_records), name="equity")
        equity_curve.index.name = "trade_date"
        positions = pd.DataFrame(
            position_rows,
            columns=["trade_date", "symbol", "shares", "available_shares",
                     "avg_cost", "weight"])
        trades = pd.DataFrame(
            trade_rows,
            columns=["trade_date", "symbol", "direction", "shares", "price",
                     "cost", "note", "capped"])
        violations = pd.DataFrame(
            violation_rows, columns=["trade_date", "symbol", "weight"])
        benchmark = self._load_benchmark(bars, cfg.benchmark)

        return BacktestResult(
            equity_curve=equity_curve,
            daily_returns=equity_curve.pct_change(),
            positions=positions,
            trades=trades,
            turnover=pd.Series(turnover_records, name="turnover"),
            costs=pd.Series(cost_records, name="costs"),
            benchmark=benchmark,
            config=cfg,
            buy_turnover=pd.Series(buy_turnover_records, name="buy_turnover"),
            sell_turnover=pd.Series(sell_turnover_records, name="sell_turnover"),
            validation_warnings=list(dict.fromkeys(warnings)),
            universe_violations=violations,
        )

    # ---- 辅助 ----
    @staticmethod
    def _enforce_universe(weights, universe) -> tuple[dict[str, float], list[tuple[str, float]]]:
        """把策略给的目标权重与当日股票池求交。

        机制层拦截：策略即使返回了宇宙外的代码（例如被剔除的 ST、科创板、
        北交所），引擎也**不会下单**，并留下可审计的违规明细。
        这层放在引擎里而不是指望策略自觉——股票池是"能买什么"的定义，
        不是给策略的参考意见。
        """
        allowed = {str(s) for s in (universe or ())}
        clean: dict[str, float] = {}
        violations: list[tuple[str, float]] = []
        for sym, w in dict(weights or {}).items():
            sym_s = str(sym)
            try:
                weight = float(w)
            except (TypeError, ValueError):
                continue
            if weight <= 0:
                continue
            if sym_s not in allowed:
                violations.append((sym_s, weight))
            else:
                clean[sym_s] = weight
        return clean, sorted(violations)
    @staticmethod
    def _tradeable_symbols(day_bars: pd.DataFrame, price_col: str) -> set[str]:
        """当日可用于定仓的标的：有行情、未停牌、参考价为正。"""
        if day_bars is None or day_bars.empty:
            return set()
        mask = day_bars[price_col].astype(float) > 0
        if "is_suspended" in day_bars.columns:
            mask &= ~day_bars["is_suspended"].astype(bool)
        return set(day_bars.loc[mask, "symbol"].astype(str))

    @staticmethod
    def _sizing_equity(state: PortfolioState, equity: float,
                       tradeable: set[str],
                       last_prices: dict[str, float],
                       last_k: dict[str, float] | None = None) -> float:
        """扣除无法交易持仓后的定仓基数。

        停牌/退市股卖不掉，这部分市值不能再拿去开新仓；若不扣除，
        目标权重会按"包含冻结市值"的总权益分配，每次调仓都现金不足。
        """
        k_map = last_k or {}
        locked = 0.0
        for sym, pos in state.positions.items():
            if sym in tradeable:
                continue
            px = float(last_prices.get(sym, pos.avg_cost))
            locked += state.position_value(sym, px, k_map.get(sym, 1.0))
        if locked <= 0:
            return float(equity)
        return max(float(equity) - locked, 0.0)

    def _check_warmup(self, trading_days: pd.DatetimeIndex,
                      signal_dates: set) -> list[str]:
        """因子暖机期不足时显式告警（否则会静默产出全现金组合）。"""
        warmup = int(getattr(self.strategy, "warmup", 0) or 0)
        if warmup <= 0 or not signal_dates:
            return []
        first_signal = min(signal_dates)
        available = int(trading_days.get_loc(first_signal)) + 1
        if available >= warmup:
            return []
        return [
            f"warmup: 策略声明暖机期 {warmup} 个交易日，但首个信号日"
            f"（{pd.Timestamp(first_signal).date()}）只有 {available} 个交易日的行情，"
            f"前若干次调仓将因因子全为 NaN 而产出空目标（全现金）。"
            f"请把回测起点前移，或缩短因子窗口。"]

    def _load_benchmark(self, bars: pd.DataFrame,
                        benchmark: Optional[str]) -> Optional[pd.Series]:
        """基准价格序列。

        **优先读 benchmark.csv**，只有在没有该文件时才退回 bars 里按代码找。
        顺序很重要：A 股指数代码与股票代码会撞车——000852 既是中证1000 指数，
        也是石化机械（深市）；000905 既是中证500 也是厦门港务。
        先查 bars 会把个股当成基准，超额和跟踪误差全错（踩过）。
        """
        if not benchmark:
            return None
        getter = getattr(self.provider, "get_benchmark", None)
        if getter is not None:
            try:
                series = getter(benchmark)
            except Exception:
                series = None
            if series is not None and len(series) > 1:
                return series
        if "symbol" in bars.columns:
            col = "adj_close" if "adj_close" in bars.columns else "close"
            bench = bars.loc[bars["symbol"] == benchmark,
                             ["trade_date", col]].dropna()
            if not bench.empty:
                series = bench.set_index("trade_date").sort_index().iloc[:, 0]
                series.name = benchmark
                return series
        return None
