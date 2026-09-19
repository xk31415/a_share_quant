"""回测引擎。

流程（每个交易日 d）：
    数据 -> 股票池 -> 因子/策略 -> 信号 -> 目标组合 -> 交易约束 -> 订单
    -> 成交模拟 -> 交易成本 -> 持仓 -> 净值 -> 绩效指标

时间对齐约定（防未来函数的核心）：
    * d 为信号日：策略只看到 trade_date <= d 的行情；
    * 目标权重于 d 收盘生成，d+1（下一交易日）开盘撮合；
    * 每个阶段（股票池/因子/策略/执行/成本/估值）都可单独测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..data.loader import DataProvider
from ..portfolio.construction import PortfolioRules, build_target_weights
from ..strategies.base import Strategy
from ..universe.builder import UniverseBuilder
from ..utils.dates import RebalanceFrequency, rebalance_dates
from .costs import CostModel
from .execution import ExecutionSimulator, generate_orders
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


@dataclass
class BacktestResult:
    """回测产出。"""

    equity_curve: pd.Series       # 日期 -> 总权益（收盘估值）
    daily_returns: pd.Series      # 日收益率
    positions: pd.DataFrame       # 每日持仓快照（含权重）
    trades: pd.DataFrame          # 逐笔成交（含受阻记录）
    turnover: pd.Series           # 调仓日双边换手率
    costs: pd.Series              # 调仓日总成本（元）
    benchmark: Optional[pd.Series] = None
    config: Optional[BacktestConfig] = None

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
                 enforce_suspension: bool = True) -> None:
        self.provider = provider
        self.strategy = strategy
        self.universe_builder = universe_builder
        self.portfolio_rules = portfolio_rules
        self.cost_model = cost_model
        self.config = config
        self.lot_size = lot_size
        self.enforce_limit = enforce_limit
        self.enforce_suspension = enforce_suspension

    def run(self) -> BacktestResult:
        cfg = self.config
        cal = self.provider.get_calendar()
        trading_days = cal.slice(cfg.start_date, cfg.end_date)
        if len(trading_days) < 2:
            raise ValueError("backtest window must span at least 2 trading days")

        bars = self.provider.get_bars(trading_days[0], trading_days[-1])
        if bars.empty:
            raise ValueError("no bars in backtest window")
        bars = bars.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        bars_by_date = {ts: g for ts, g in bars.groupby("trade_date")}
        signal_dates = set(rebalance_dates(trading_days, cfg.rebalance_frequency))

        state = PortfolioState(cfg.initial_capital)
        simulator = ExecutionSimulator(
            self.cost_model, lot_size=self.lot_size,
            enforce_limit=self.enforce_limit,
            enforce_suspension=self.enforce_suspension)

        last_prices: dict[str, float] = {}
        pending_weights: Optional[dict[str, float]] = None
        prev_equity = cfg.initial_capital

        equity_records: list[tuple[pd.Timestamp, float]] = []
        position_rows: list[dict] = []
        trade_rows: list[dict] = []
        turnover_records: dict[pd.Timestamp, float] = {}
        cost_records: dict[pd.Timestamp, float] = {}

        for d in trading_days:
            day_bars = bars_by_date.get(d, bars.iloc[0:0])

            # --- 开盘前：T+1 释放昨日及更早持有的份额 ---
            state.start_of_day()

            # --- 开盘：执行上一信号日生成的目标权重（当日开盘价撮合） ---
            if pending_weights is not None and not day_bars.empty:
                ref_prices = (
                    day_bars.set_index("symbol")[cfg.execution_price]
                    .astype(float).to_dict()
                )
                orders = generate_orders(pending_weights, state, prev_equity,
                                         ref_prices, self.lot_size)
                fills = simulator.execute(orders, day_bars, state)
                day_turnover = 0.0
                day_cost = 0.0
                for f in fills:
                    trade_rows.append({
                        "trade_date": d, "symbol": f.symbol,
                        "direction": f.direction, "shares": f.shares,
                        "price": f.price, "cost": f.cost, "note": f.note})
                    day_turnover += f.shares * f.price
                    day_cost += f.cost
                if fills and (day_turnover > 0 or day_cost > 0):
                    turnover_records[d] = day_turnover / max(prev_equity, 1e-12)
                    cost_records[d] = day_cost
                pending_weights = None

            # --- 收盘估值（收盘价缺失则沿用最近价） ---
            if not day_bars.empty:
                for sym, px in day_bars.set_index("symbol")["close"].items():
                    if pd.notna(px):
                        last_prices[sym] = float(px)
            equity = state.market_value(last_prices)
            equity_records.append((d, equity))
            for sym, pos in sorted(state.positions.items()):
                px = last_prices.get(sym, pos.avg_cost)
                position_rows.append({
                    "trade_date": d, "symbol": sym, "shares": pos.shares,
                    "available_shares": pos.available_shares,
                    "avg_cost": round(pos.avg_cost, 6),
                    "weight": (pos.shares * px / equity) if equity > 0 else 0.0})

            # --- 信号日收盘：生成次日执行的目标权重 ---
            if d in signal_dates:
                hist = bars.loc[bars["trade_date"] <= d]
                universe = self.universe_builder.get_universe(d)
                target = self.strategy.generate_target(d, hist, universe)
                pending_weights = build_target_weights(target.weights,
                                                       self.portfolio_rules)
            prev_equity = equity

        equity_curve = pd.Series(dict(equity_records), name="equity")
        equity_curve.index.name = "trade_date"
        positions = pd.DataFrame(
            position_rows,
            columns=["trade_date", "symbol", "shares", "available_shares",
                     "avg_cost", "weight"])
        trades = pd.DataFrame(
            trade_rows,
            columns=["trade_date", "symbol", "direction", "shares", "price",
                     "cost", "note"])
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
        )

    @staticmethod
    def _load_benchmark(bars: pd.DataFrame,
                        benchmark: Optional[str]) -> Optional[pd.Series]:
        """若基准指数在行情数据中，返回其价格序列用于对照。"""
        if not benchmark:
            return None
        bench = bars.loc[bars["symbol"] == benchmark, ["trade_date", "adj_close"]]
        if bench.empty:
            bench = bars.loc[bars["symbol"] == benchmark, ["trade_date", "close"]]
        if bench.empty:
            return None
        series = bench.set_index("trade_date").sort_index().iloc[:, 0]
        series.name = benchmark
        return series
