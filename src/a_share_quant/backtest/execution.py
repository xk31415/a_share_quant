"""订单生成与成交模拟（A 股约束：T+1、涨跌停、停牌、整手）。

数据来源约定（重要）：
  * 停牌/ST 以 **status 表为准**，引擎会在启动时把 status 合并进 bars
    （见 ``BacktestEngine._attach_status``）；bars 上的同名列只作兜底；
  * 涨跌停以 bars 的 ``limit_up/limit_down`` 为准，缺列时引擎启动即报错
    （不允许"配置写了 true 但静默不生效"）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pandas as pd

from .costs import CostModel
from .positions import PortfolioState


@dataclass(frozen=True)
class Order:
    symbol: str
    direction: str  # "buy" | "sell"
    shares: int


@dataclass
class Fill:
    symbol: str
    direction: str
    shares: int    # 实际成交股数（0 = 未成交）
    price: float   # 实际成交价（含滑点）
    cost: float    # 交易成本
    note: str = ""


def generate_orders(target_weights: Mapping[str, float],
                    portfolio: PortfolioState,
                    equity: float,
                    ref_prices: Mapping[str, float],
                    lot_size: int = 100,
                    cash_buffer: float = 0.0) -> list[Order]:
    """把目标权重转换为整手订单（先卖后买，释放现金）。

    * 可用于建仓的权益 = equity * (1 - cash_buffer)；缓冲用于覆盖佣金、印花税与
      滑点，避免"目标权重合计 100% 导致最后一笔买单现金不足"；
    * 目标股数 = floor(weight * investable / ref_price / lot) * lot；
    * 持仓不在目标中 -> 全部卖出（零股可一次性卖出，符合 A 股规则）；
    * 无成交日参考价的标的跳过（如停牌），留待下一调仓日再平衡。
    ref_prices 必须是成交日价格（由调用方在成交时点传入）。
    """
    if lot_size < 1:
        raise ValueError("lot_size must be >= 1")
    if not 0.0 <= cash_buffer < 1.0:
        raise ValueError("cash_buffer must be in [0, 1)")
    targets = {str(s): float(w) for s, w in dict(target_weights or {}).items()
               if float(w) > 0}
    investable = float(equity) * (1.0 - cash_buffer)

    sell_orders: list[Order] = []
    # 1) 清仓：持仓不在目标中
    for sym in sorted(portfolio.positions):
        pos = portfolio.positions[sym]
        if pos.shares > 0 and sym not in targets:
            sell_orders.append(Order(sym, "sell", int(pos.shares)))

    # 2) 再平衡：目标内标的调向目标股数
    buy_orders: list[Order] = []
    for sym, w in sorted(targets.items()):
        price = ref_prices.get(sym)
        if price is None or price <= 0:
            continue
        cur = portfolio.positions.get(sym)
        cur_shares = int(cur.shares) if cur is not None else 0
        desired = int((w * investable) // (price * lot_size)) * lot_size
        if desired > cur_shares:
            add = (desired - cur_shares) // lot_size * lot_size
            if add > 0:
                buy_orders.append(Order(sym, "buy", int(add)))
        elif desired < cur_shares:
            if desired == 0:
                sell_orders.append(Order(sym, "sell", int(cur_shares)))
            else:
                cut = (cur_shares - desired) // lot_size * lot_size
                if cut > 0:
                    sell_orders.append(Order(sym, "sell", int(cut)))
    return sell_orders + buy_orders


class ExecutionSimulator:
    """按成交日行情撮合订单，执行 A 股交易约束。

    约束（均可配置开关）：
      * 停牌（is_suspended 或无行情行）-> 不成交；
      * 开盘价 >= 涨停价 -> 买入不成交（保守假设：一字涨停买不到）；
      * 开盘价 <= 跌停价 -> 卖出不成交；
      * 买入按整手递减直至现金可负担；**现金不足时按比例同时缩减全部买单**，
        而不是按顺序把靠后的标的整笔丢弃（后者会造成与代码字典序相关的偏差）。
    """

    def __init__(self, cost_model: CostModel, lot_size: int = 100,
                 enforce_limit: bool = True,
                 enforce_suspension: bool = True) -> None:
        self.cost_model = cost_model
        self.lot_size = lot_size
        self.enforce_limit = enforce_limit
        self.enforce_suspension = enforce_suspension

    # ---- 内部工具 ----
    @staticmethod
    def _row_for(bars: pd.DataFrame, symbol: str):
        if symbol not in bars.index:
            return None
        row = bars.loc[symbol]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return row

    def execute(self, orders: Sequence[Order], exec_day_bars: pd.DataFrame,
                portfolio: PortfolioState) -> list[Fill]:
        """撮合订单。exec_day_bars 为成交日行情（long format）。"""
        if exec_day_bars is None or exec_day_bars.empty:
            return [Fill(o.symbol, o.direction, 0, 0.0, 0.0, "blocked:no_bar")
                    for o in orders]
        bars = exec_day_bars.set_index("symbol")
        fills: list[Fill] = []

        sells = [o for o in orders if o.direction == "sell"]
        buys = [o for o in orders if o.direction == "buy"]
        unknown = [o for o in orders if o.direction not in ("buy", "sell")]
        if unknown:
            raise ValueError(f"unknown order direction: {unknown[0].direction!r}")

        # ---- 1) 先卖：释放现金（T+0 资金可用）----
        for od in sells:
            row = self._row_for(bars, od.symbol)
            if row is None:
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0, "blocked:no_bar"))
                continue
            if self.enforce_suspension and bool(row.get("is_suspended", False)):
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0, "blocked:suspended"))
                continue
            open_px = float(row["open"])
            limit_down = row.get("limit_down")
            if (self.enforce_limit and pd.notna(limit_down)
                    and open_px <= float(limit_down) + 1e-9):
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0, "blocked:limit_down"))
                continue
            pos = portfolio.positions.get(od.symbol)
            shares = min(int(od.shares),
                         int(pos.available_shares) if pos is not None else 0)
            if shares <= 0:
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0,
                                  "blocked:no_available_shares"))
                continue
            price = self.cost_model.sell_price(open_px)
            turnover = shares * price
            cost = self.cost_model.sell_cost(turnover)
            portfolio.sell(od.symbol, shares, price, cost)
            fills.append(Fill(od.symbol, "sell", shares, price, cost, "filled"))

        # ---- 2) 再买：先剔除不可买订单，再按现金统一缩放 ----
        eligible: list[tuple[Order, float]] = []
        for od in buys:
            row = self._row_for(bars, od.symbol)
            if row is None:
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0, "blocked:no_bar"))
                continue
            if self.enforce_suspension and bool(row.get("is_suspended", False)):
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0, "blocked:suspended"))
                continue
            open_px = float(row["open"])
            limit_up = row.get("limit_up")
            if (self.enforce_limit and pd.notna(limit_up)
                    and open_px >= float(limit_up) - 1e-9):
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0, "blocked:limit_up"))
                continue
            eligible.append((od, self.cost_model.buy_price(open_px)))

        need = sum(int(od.shares) * px + self.cost_model.buy_cost(int(od.shares) * px)
                   for od, px in eligible)
        cash = float(portfolio.cash)
        # 现金不足 -> 全部买单等比例缩减（留 0.1% 余量吸收浮点误差）
        factor = 1.0 if need <= cash else max(cash / need * 0.999, 0.0)

        for od, price in eligible:
            shares = int(int(od.shares) * factor) // self.lot_size * self.lot_size
            turnover = shares * price
            cost = self.cost_model.buy_cost(turnover)
            while shares > 0 and turnover + cost > cash + 1e-6:
                shares -= self.lot_size
                turnover = shares * price
                cost = self.cost_model.buy_cost(turnover)
            if shares <= 0:
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0,
                                  "blocked:insufficient_cash"))
                continue
            portfolio.buy(od.symbol, shares, price, cost)
            cash -= turnover + cost
            fills.append(Fill(od.symbol, "buy", shares, price, cost, "filled"))
        return fills
