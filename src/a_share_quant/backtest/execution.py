"""订单生成与成交模拟（A 股约束：T+1、涨跌停、停牌、整手）。

数据来源约定（重要）：
  * 停牌/ST 以 **status 表为准**，引擎会在启动时把 status 合并进 bars
    （见 ``BacktestEngine._attach_status``）；bars 上的同名列只作兜底；
  * 涨跌停以 bars 的 ``limit_up/limit_down`` 为准，缺列时引擎启动即报错
    （不允许"配置写了 true 但静默不生效"）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import pandas as pd

from .costs import CostModel
from .positions import PortfolioState


@dataclass(frozen=True)
class Order:
    symbol: str
    direction: str  # "buy" | "sell"
    shares: int
    reason: str = ""   # 非空表示"本来就下不出去"，直接记成该原因的 0 股记录


@dataclass
class Fill:
    symbol: str
    direction: str
    shares: int    # 实际成交股数（0 = 未成交）
    price: float   # 实际成交价（含滑点）
    cost: float    # 交易成本
    note: str = ""
    capped: bool = False   # 是否因成交量上限被缩减


def generate_orders(target_weights: Mapping[str, float],
                    portfolio: PortfolioState,
                    equity: float,
                    ref_prices: Mapping[str, float],
                    lot_size: int = 100,
                    cash_buffer: float = 0.0,
                    target_shares: Optional[dict[str, int]] = None) -> list[Order]:
    """把目标权重转换为整手订单（先卖后买，释放现金）。

    * 可用于建仓的权益 = equity * (1 - cash_buffer)；缓冲用于覆盖佣金、印花税与
      滑点，避免"目标权重合计 100% 导致最后一笔买单现金不足"；
    * 目标股数 = floor(weight * investable / ref_price / lot) * lot；
    * 持仓不在目标中 -> 全部卖出（零股可一次性卖出，符合 A 股规则）；
    * 无成交日参考价的标的跳过（如停牌），留待下一调仓日再平衡。
    ref_prices 必须是成交日价格（由调用方在成交时点传入）。

    ``target_shares``（可选出参）：把每个目标标的的**目标股数**填进去，
    供引擎判断"这次到底建满了没有"。不传时行为与旧版逐位一致。
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
            # 目标里有、但成交日没有行情（退市/长期停牌）——必须留下记录，
            # 不能 continue 掉，否则 trades.csv 里看不出"本来该买/该卖"。
            # 方向按真实意图定：已持仓说明本该减仓，未持仓才是在建仓。
            cur_held = portfolio.positions.get(sym)
            if cur_held is not None and cur_held.shares > 0:
                sell_orders.append(Order(sym, "sell", 0, reason="blocked:no_bar"))
            else:
                buy_orders.append(Order(sym, "buy", 0, reason="blocked:no_bar"))
            continue
        cur = portfolio.positions.get(sym)
        cur_shares = int(cur.shares) if cur is not None else 0
        desired = int((w * investable) // (price * lot_size)) * lot_size
        if target_shares is not None and desired > 0:
            target_shares[sym] = desired
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
      * 成交参考价 >= 涨停价 -> 买入不成交（默认参考价是开盘价：一字涨停买不到）；
      * 成交参考价 <= 跌停价 -> 卖出不成交；
      * 买入按整手递减直至现金可负担；**现金不足时按比例同时缩减全部买单**，
        而不是按顺序把靠后的标的整笔丢弃（后者会造成与代码字典序相关的偏差）。
    """

    def __init__(self, cost_model: CostModel, lot_size: int = 100,
                 enforce_limit: bool = True,
                 enforce_suspension: bool = True,
                 max_participation_rate: float = 0.0,
                 price_col: str = "open") -> None:
        self.cost_model = cost_model
        self.lot_size = lot_size
        self.enforce_limit = enforce_limit
        self.enforce_suspension = enforce_suspension
        # 单笔成交不超过当日成交量的比例（0 = 不限制）。容量约束：
        # 没有它，100 万本金也能"买下"全天只有 100 股成交的票。
        self.max_participation_rate = float(max_participation_rate)
        if not 0.0 <= self.max_participation_rate <= 1.0:
            raise ValueError("max_participation_rate must be in [0, 1]")
        # 默认 open：与历史回测逐位一致。close 只在调用方显式指定时使用，
        # 涨跌停是否封板也看这同一列，不另换一根价格。
        if price_col not in ("open", "close"):
            raise ValueError("price_col must be 'open' or 'close'")
        self.price_col = price_col

    # ---- 内部工具 ----
    @staticmethod
    def _row_for(bars: pd.DataFrame, symbol: str):
        if symbol not in bars.index:
            return None
        row = bars.loc[symbol]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return row

    def _volume_cap(self, row) -> Optional[int]:
        """当日可成交股数上限（**未取整**）。

        返回值：
          * ``None`` —— 未启用（``max_participation_rate <= 0``）
          * ``-1``   —— 当日成交量缺失（NaN），无法判断容量，调用方必须拦截
          * ``>=0``  —— 上限股数

        刻意不在这里按整手取整：是否取整取决于"上限有没有真正绑定"。
        若上限 >= 委托股数，委托原样成交（清仓卖零股本就该一次卖掉）；
        只有上限真的卡住委托时，才把成交股数压到整手——否则会把
        本可成交的零股清仓截成整手、留下残仓（F-15）。
        ``volume`` 是必填列，缺失属于数据异常，不能当作"没有上限"放行。
        """
        if self.max_participation_rate <= 0:
            return None
        vol = row.get("volume")
        if vol is None or pd.isna(vol):
            return -1
        return max(int(float(vol) * self.max_participation_rate), 0)

    def execute(self, orders: Sequence[Order], exec_day_bars: pd.DataFrame,
                portfolio: PortfolioState,
                adj_factors: Optional[Mapping[str, float]] = None) -> list[Fill]:
        """撮合订单。exec_day_bars 为成交日行情（long format）。

        ``adj_factors`` 为当日各标的的复权因子（``adj_close/close``），
        买入时记入持仓作为估值基准；缺省视为 1（无公司行动）。
        """
        k_map = adj_factors or {}
        if exec_day_bars is None or exec_day_bars.empty:
            return [Fill(o.symbol, o.direction, 0, 0.0, 0.0,
                         o.reason or "blocked:no_bar") for o in orders]
        bars = exec_day_bars.set_index("symbol")
        fills: list[Fill] = []

        sells = [o for o in orders if o.direction == "sell"]
        buys = [o for o in orders if o.direction == "buy"]
        unknown = [o for o in orders if o.direction not in ("buy", "sell")]
        if unknown:
            raise ValueError(f"unknown order direction: {unknown[0].direction!r}")

        # ---- 1) 先卖：释放现金（T+0 资金可用）----
        for od in sells:
            if od.reason:   # 下单前就知道做不成（例如目标内无行情），留痕
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0, od.reason))
                continue
            row = self._row_for(bars, od.symbol)
            if row is None:
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0, "blocked:no_bar"))
                continue
            if self.enforce_suspension and bool(row.get("is_suspended", False)):
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0, "blocked:suspended"))
                continue
            open_px = float(row[self.price_col])
            limit_down = row.get("limit_down")
            exempt = bool(row.get("limit_exempt", False))
            if self.enforce_limit and not exempt:
                if pd.isna(limit_down):
                    fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0,
                                      "blocked:no_limit_data"))
                    continue
                if open_px <= float(limit_down) + 1e-9:
                    fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0,
                                      "blocked:limit_down"))
                    continue
            pos = portfolio.positions.get(od.symbol)
            shares = min(int(od.shares),
                         int(pos.available_shares) if pos is not None else 0)
            if shares <= 0:
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0,
                                  "blocked:no_available_shares"))
                continue
            # 卖出也按整手约束：持有整百股时，申报数量必须是 100 股整数倍
            #（上证函〔2014〕301 号第一条（一）、深证会〔2014〕72 号）。
            # 注意：只有上限真的卡住委托时才取整；能一次卖完的零股清仓不受影响。
            cap = self._volume_cap(row)
            if cap == -1:
                fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0,
                                  "blocked:no_volume_data"))
                continue
            capped = cap is not None and shares > cap
            if capped:
                shares = (cap // self.lot_size) * self.lot_size
                if shares <= 0:
                    fills.append(Fill(od.symbol, "sell", 0, 0.0, 0.0,
                                      "blocked:volume"))
                    continue
            price = self.cost_model.sell_price(open_px)
            turnover = shares * price
            cost = self.cost_model.sell_cost(turnover)
            # 现金回笼按复权口径（R08 F-40）：估值走 k/adj_ref，卖出也必须走，
            # 否则送转/分红之后一卖出就凭空亏损。`cost` 仍按原始成交额，见 sell()。
            portfolio.sell(od.symbol, shares, price, cost,
                           row.get("adj_factor", 1.0))
            fills.append(Fill(od.symbol, "sell", shares, price, cost, "filled",
                              capped=capped))

        # ---- 2) 再买：先剔除不可买订单，再按现金统一缩放 ----
        eligible: list[tuple[Order, float, Optional[int]]] = []
        for od in buys:
            if od.reason:
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0, od.reason))
                continue
            row = self._row_for(bars, od.symbol)
            if row is None:
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0, "blocked:no_bar"))
                continue
            if self.enforce_suspension and bool(row.get("is_suspended", False)):
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0, "blocked:suspended"))
                continue
            open_px = float(row[self.price_col])
            limit_up = row.get("limit_up")
            exempt = bool(row.get("limit_exempt", False))
            if self.enforce_limit and not exempt:
                if pd.isna(limit_up):
                    fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0,
                                      "blocked:no_limit_data"))
                    continue
                if open_px >= float(limit_up) - 1e-9:
                    fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0,
                                      "blocked:limit_up"))
                    continue
            cap = self._volume_cap(row)
            if cap == -1:
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0,
                                  "blocked:no_volume_data"))
                continue
            eligible.append((od, self.cost_model.buy_price(open_px), cap))

        cash = float(portfolio.cash)

        # 先算出每笔的目标股数，再决定怎么分钱。
        plan: list[list] = []      # [od, price, cap, want, shares, volume_bound]
        for od, price, cap in eligible:
            if cap is not None and cap <= 0:
                plan.append([od, price, cap, 0, 0, True])
                continue
            volume_bound = cap is not None and int(od.shares) > cap
            want = int(od.shares) if cap is None else min(int(od.shares), cap)
            plan.append([od, price, cap, want, 0, volume_bound])

        need = sum(w * px + self.cost_model.buy_cost(w * px)
                   for _, px, _, w, _, _ in plan if w > 0)
        # 现金不足 -> 全部买单等比例缩减（留 0.1% 余量吸收浮点误差）
        factor = 1.0 if need <= cash else max(cash / need * 0.999, 0.0)
        for item in plan:
            item[4] = int(item[3] * factor) // self.lot_size * self.lot_size

        # 整手取整会把零头留在账上（50 只票每次能剩几万元），如果再按顺序扣现金，
        # 后面的订单就会被压成 0 股——账上还有钱却没买满。这里按轮转把零头补回去。
        spent = sum(s * px + self.cost_model.buy_cost(s * px)
                    for _, px, _, _, s, _ in plan if s > 0)
        leftover = cash - spent
        changed = True
        while changed:
            changed = False
            for item in plan:
                od, px, _, want, s, _ = item
                if s + self.lot_size > want:
                    continue
                add = self.lot_size * px
                extra = (self.cost_model.buy_cost((s + self.lot_size) * px)
                         - self.cost_model.buy_cost(s * px))
                if add + extra <= leftover + 1e-9:
                    item[4] = s + self.lot_size
                    leftover -= add + extra
                    changed = True

        for od, price, cap, want, shares, volume_bound in plan:
            if shares <= 0:
                reason = ("blocked:volume"
                          if volume_bound or (cap is not None and cap <= 0)
                          else "blocked:insufficient_cash")
                fills.append(Fill(od.symbol, "buy", 0, 0.0, 0.0, reason))
                continue
            turnover = shares * price
            cost = self.cost_model.buy_cost(turnover)
            portfolio.buy(od.symbol, shares, price, cost,
                          adj_factor=k_map.get(od.symbol, 1.0))
            cash -= turnover + cost
            fills.append(Fill(od.symbol, "buy", shares, price, cost, "filled",
                              capped=volume_bound))
        return fills
