"""实盘信号层：把「固定样本 + 仓位控制」落成可执行的下单清单。

前提（见 `reports/live_readiness.md`）：三段样本外检验下来，选股与择时都没有
通过门槛，唯一站得住的方案是「等权持有 N=100~200 只 + 按仓位控制回撤」。
所以本模块**不做选股**，只做三件事：

1. 维护一个**冻结的样本池**：新上市股票永不进入，退市/ST 由备用名次补位；
2. 按目标权重算出每只该有多少股（整手约束 + 现金约束 + 余量轮转补手）；
3. 与当前持仓差分，只列出**需要动的单**，小额偏离不动（省佣金）。

为什么样本必须冻结：股票池每个月都在变（新股上市、退市），
``sorted(pool, key=sha1)`` 的截断位置会随之改变，等价于每次重新抽一次。
实测这会把这 50 只组合整体换掉，年化差 1.8pp（全部是抽样噪声）——
和 seed 参数化那个坑是同一个因果链。

为什么不复用 `strategies.EqualWeightStrategy`：那是**回测**用的，
每个调仓日重新按当时的宇宙抽样；实盘需要的是「第一次抽完就固定」，
两者的样本语义相反，混用会让「回测过的东西」和「实际下的单」不是一回事。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import pandas as pd

DEFAULT_LOT_SIZE = 100
# 单笔金额低于此值不下单：100 万 / 100 只 = 1 万/只，
# 2000 元约占 20%，再小的偏离调仓收益抵不过最低佣金 5 元 + 双边价差。
DEFAULT_MIN_TRADE_VALUE = 2000.0


def _stable_key(symbol: str, seed: int = 0) -> str:
    """与运行环境无关的稳定散列。

    ``seed=0`` 沿用 ``sha1(symbol)``——与 ``strategies.equal_weight`` 同一口径，
    这样实盘样本和回测样本可以互相验证。加 seed 时必须只影响 seed>0 的分支。
    """
    payload = f"{seed}:{symbol}" if seed else str(symbol)
    return hashlib.sha1(payload.encode()).hexdigest()


def rank_universe(universe: Iterable[str], seed: int = 0) -> list[str]:
    """按稳定散列排序，等价于无放回随机排列（可复现）。"""
    return sorted({str(s) for s in universe}, key=lambda s: _stable_key(s, seed))


# ---------------------------------------------------------------- 样本冻结
@dataclass
class Sample:
    """冻结的样本池。

    ``ranked`` 是**完整排序**而不是只存前 N：当某只退市或被剔除时，
    按名次顺位补人。这样「换人」是确定的，不需要重新抽一次。
    """

    ranked: list[str]
    n: int
    seed: int = 0
    created: str = ""
    data_dir: str = ""
    as_of: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.n < 1:
            raise ValueError("n must be >= 1")
        if len(self.ranked) < self.n:
            raise ValueError(f"ranked 只有 {len(self.ranked)} 只，不足 n={self.n}")

    def active(self, investable: Optional[Iterable[str]] = None,
               n: Optional[int] = None) -> tuple[list[str], list[str]]:
        """返回 (入选名单, 被跳过名单)。

        ``investable`` 为 None 表示不检查（用于刚建样本时）。
        """
        want = self.n if n is None else int(n)
        if investable is None:
            return list(self.ranked[:want]), []
        allow = {str(s) for s in investable}
        picked: list[str] = []
        skipped: list[str] = []
        for sym in self.ranked:
            if len(picked) >= want:
                break
            if sym in allow:
                picked.append(sym)
            else:
                skipped.append(sym)
        return picked, skipped

    def to_json(self) -> dict:
        return {
            "created": self.created, "data_dir": self.data_dir,
            "as_of": self.as_of, "n": self.n, "seed": self.seed,
            "note": self.note, "ranked": list(self.ranked),
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=1),
                     encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Sample":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            ranked=[str(s) for s in raw["ranked"]],
            n=int(raw["n"]),
            seed=int(raw.get("seed", 0)),
            created=str(raw.get("created", "")),
            data_dir=str(raw.get("data_dir", "")),
            as_of=str(raw.get("as_of", "")),
            note=str(raw.get("note", "")),
        )


# ---------------------------------------------------------------- 定量
def size_positions(symbols: Sequence[str], prices: Mapping[str, float],
                   investable: float, lot_size: int = DEFAULT_LOT_SIZE,
                   max_rounds: int = 200) -> dict[str, int]:
    """把 ``investable`` 元等权分配到 symbols，返回每只的目标股数。

    先按每只预算整手下取，再做**余量轮转补手**：把整手取整剩下的钱按
    「缺口从大到小」逐手加回去，直到没有一只再加得起一手。

    不做这一步的后果实测过：50 只票每只向下取整浪费 1000~3000 元，
    累积几万元躺在账上，同时尾部买单被压成 0 股（`blocked:insufficient_cash`
    107 次 → 26 次就是这一处）。
    """
    symbols = [str(s) for s in symbols]
    if not symbols:
        return {}
    if investable <= 0:
        return {s: 0 for s in symbols}
    missing = [s for s in symbols if not prices.get(s) or prices[s] <= 0]
    if missing:
        raise ValueError(f"缺少有效价格：{missing[:5]}")

    per = float(investable) / len(symbols)
    shares = {s: int(per / (float(prices[s]) * lot_size)) * lot_size for s in symbols}
    spent = sum(shares[s] * float(prices[s]) for s in symbols)
    remaining = float(investable) - spent

    for _ in range(max_rounds):
        gaps = sorted(symbols, key=lambda s: per - shares[s] * float(prices[s]),
                      reverse=True)
        added = False
        for sym in gaps:
            lot_cost = float(prices[sym]) * lot_size
            if lot_cost <= remaining + 1e-9:
                shares[sym] += lot_size
                remaining -= lot_cost
                added = True
        if not added or remaining <= 0:
            break
    return shares


def diff_orders(target: Mapping[str, int], current: Mapping[str, int],
                prices: Mapping[str, float], lot_size: int = DEFAULT_LOT_SIZE,
                min_trade_value: float = DEFAULT_MIN_TRADE_VALUE) -> list[dict]:
    """目标持仓 vs 当前持仓 → 下单清单。

    交易规则（上证函〔2014〕301 号、深证会〔2014〕72 号）：
      * 买入必须是 lot_size 的整数倍；
      * **卖出也必须是整数倍**（持有整百股时），不得拆出零股；
      * 唯一的例外是**清仓**——剩余零股可以一次性卖出。

    所以 ``target=0`` 时直接卖出全部持股（含零股），其余情况按整手向下取整。
    """
    symbols = sorted(set(target) | set(current))
    orders: list[dict] = []
    for sym in symbols:
        px = prices.get(sym)
        cur = int(current.get(sym, 0) or 0)
        tgt = int(target.get(sym, 0) or 0)
        if cur == tgt:
            continue
        if px is None or px <= 0:
            if tgt != cur:
                orders.append({"symbol": sym, "side": "hold", "shares": 0,
                               "price": float("nan"), "amount": 0.0,
                               "note": "blocked:no_price"})
            continue
        px = float(px)
        delta = tgt - cur
        if delta > 0:
            shares = delta // lot_size * lot_size
            if shares <= 0:
                continue
            amount = shares * px
            if amount < min_trade_value:
                continue
            orders.append({"symbol": sym, "side": "buy", "shares": shares,
                           "price": px, "amount": amount, "note": "rebalance"})
        else:
            if tgt == 0:
                shares = cur                      # 清仓：允许带零股
                note = "clear"
            else:
                shares = (-delta) // lot_size * lot_size
                note = "rebalance"
                if shares <= 0:
                    continue
            amount = shares * px
            if amount < min_trade_value and tgt != 0:
                continue
            orders.append({"symbol": sym, "side": "sell", "shares": shares,
                           "price": px, "amount": amount, "note": note})
    return orders


# ---------------------------------------------------------------- 计划
@dataclass
class LivePlan:
    as_of: pd.Timestamp
    equity: float
    investable: float
    position: float
    n_target: int
    holdings: pd.DataFrame = field(default_factory=pd.DataFrame)
    target: pd.DataFrame = field(default_factory=pd.DataFrame)
    orders: pd.DataFrame = field(default_factory=pd.DataFrame)
    warnings: list[str] = field(default_factory=list)

    @property
    def buy_amount(self) -> float:
        if self.orders.empty:
            return 0.0
        return float(self.orders.loc[self.orders["side"] == "buy", "amount"].sum())

    @property
    def sell_amount(self) -> float:
        if self.orders.empty:
            return 0.0
        return float(self.orders.loc[self.orders["side"] == "sell", "amount"].sum())

    def to_json(self) -> dict:
        return {
            "as_of": str(self.as_of.date()),
            "equity": round(self.equity, 2),
            "investable": round(self.investable, 2),
            "position": self.position,
            "n_target": self.n_target,
            "n_orders": int(len(self.orders)),
            "buy_amount": round(self.buy_amount, 2),
            "sell_amount": round(self.sell_amount, 2),
            "warnings": list(self.warnings),
        }


def build_plan(as_of, prices: Mapping[str, float], sample: Sample,
               investable_universe: Iterable[str],
               current_holdings: Optional[Mapping[str, int]] = None,
               capital: float = 1_000_000.0,
               position: float = 1.0,
               cash_buffer: float = 0.01,
               lot_size: int = DEFAULT_LOT_SIZE,
               min_trade_value: float = DEFAULT_MIN_TRADE_VALUE,
               n: Optional[int] = None,
               tradeable: Optional[Iterable[str]] = None) -> LivePlan:
    """生成某日的目标持仓与下单清单（纯函数，不碰数据源）。

    参数
    ----
    as_of                 : 信号日（收盘后计算，次日开盘执行）
    prices                : 代码 -> 定价基准（收盘价）。停牌股要给**最后一个收盘价**，
                            否则总资产会被低估
    sample                : 冻结样本
    investable_universe   : 当日可投资代码集合（已剔除 ST/停牌/退市）
    current_holdings      : 代码 -> 持股数；None 表示空仓建仓
    capital               : 账户总资产（元）。有持仓时用它反推现金，而不是另传
    position              : 目标仓位 w（0~1）。回撤 ≈ w × 满仓回撤
    cash_buffer           : 预留现金比例（覆盖佣金/印花税/滑点）
    tradeable             : 当日可交易代码（默认 = investable_universe）。
                            不在其中的持仓**冻结**：算进仓位、不下单
    """
    if not 0 < position <= 1:
        raise ValueError("position must be in (0, 1]")
    if capital <= 0:
        raise ValueError("capital must be > 0")

    ts = pd.Timestamp(as_of).normalize()
    holdings = {str(k): int(v) for k, v in (current_holdings or {}).items() if int(v) > 0}
    can_trade = {str(s) for s in (tradeable if tradeable is not None
                                  else investable_universe)}

    warnings: list[str] = []
    held_rows = []
    held_value = 0.0
    for sym, sh in sorted(holdings.items()):
        px = prices.get(sym)
        if px is None or px <= 0:
            raise ValueError(
                f"持仓 {sym} 缺当日/最后收盘价，无法估值总资产——"
                f"请确认数据覆盖到该标的最后交易日（不允许静默按 0 处理）")
        val = sh * float(px)
        held_value += val
        held_rows.append({"symbol": sym, "shares": sh, "price": float(px),
                          "value": val, "tradeable": sym in can_trade})
    equity = float(capital)
    cash = equity - held_value
    if cash < -1e-6:
        warnings.append(
            f"现金为负（{cash:,.0f} 元）：--capital 低于当前持仓市值，"
            f"请用总资产口径重新调用")

    picked, skipped = sample.active(investable_universe, n=n)
    want = n or sample.n
    if skipped:
        warnings.append(
            f"样本池里有 {len(skipped)} 只已不可投资，按名次顺位补人：{skipped[:8]}"
            + ("..." if len(skipped) > 8 else ""))
    if len(picked) < want:
        warnings.append(
            f"可投资样本只有 {len(picked)} 只，少于目标 {want} 只——"
            f"样本池需要扩容（--pool 调大）")

    stale = [s for s in holdings if s not in set(picked)]
    if stale:
        warnings.append(f"{len(stale)} 只持仓不在当前样本内，目标为 0（清仓）：{stale[:8]}")

    # 冻结：不在 picked 里又不能交易的持仓，保持原股数，占用仓位不下单
    frozen = {s: h for s, h in holdings.items() if s not in can_trade}
    frozen_value = sum(h * float(prices[s]) for s, h in frozen.items())
    if frozen:
        warnings.append(
            f"{len(frozen)} 只持仓当日不可交易（停牌/跌停/退市整理），"
            f"保持原股数、占用仓位 {frozen_value:,.0f} 元：{list(frozen)[:8]}")

    # 预算：总仓位目标减去冻结部分，再留缓冲
    budget = max(equity * float(position) - frozen_value, 0.0) * (1.0 - float(cash_buffer))
    picked_tradeable = [s for s in picked if s in can_trade]
    target_shares = size_positions(picked_tradeable, prices, budget, lot_size) \
        if picked_tradeable else {}
    for sym, sh in frozen.items():
        target_shares[sym] = int(sh)          # 冻结持仓的目标就是现状

    orders = diff_orders(target_shares, holdings, prices, lot_size, min_trade_value)
    # 不可交易的标的：把任何要动的单改成"冻结"提示，避免下达无法成交的委托
    for od in orders:
        if od["symbol"] in frozen and od["note"] != "hold":
            od.update(side="hold", shares=0, amount=0.0, note="blocked:not_tradeable")

    tgt_rows = []
    for sym in picked:
        sh = int(target_shares.get(sym, 0))
        px = float(prices[sym])
        tgt_rows.append({"symbol": sym, "price": px, "target_shares": sh,
                         "target_value": round(sh * px, 2),
                         "weight": round((sh * px / equity), 6) if equity > 0 else 0.0,
                         "current_shares": int(holdings.get(sym, 0)),
                         "tradeable": sym in can_trade})
    # 冻结持仓（停牌/跌停/退市整理）也要进目标表，否则 target.csv
    # 不是"完整的目标持仓"，对账时会少几只
    for sym, sh in sorted(frozen.items()):
        if sym in set(picked):
            continue
        px = float(prices[sym])
        tgt_rows.append({"symbol": sym, "price": px, "target_shares": int(sh),
                         "target_value": round(int(sh) * px, 2),
                         "weight": round(int(sh) * px / equity, 6) if equity > 0 else 0.0,
                         "current_shares": int(sh), "tradeable": False})
    target_df = pd.DataFrame(tgt_rows)
    orders_df = pd.DataFrame(orders, columns=["symbol", "side", "shares",
                                              "price", "amount", "note"])

    plan = LivePlan(as_of=ts, equity=equity, investable=budget,
                    position=float(position), n_target=len(picked),
                    holdings=pd.DataFrame(held_rows), target=target_df,
                    orders=orders_df, warnings=warnings)

    available = cash + plan.sell_amount
    if plan.buy_amount > available + 1e-6:
        warnings.append(
            f"买入需 {plan.buy_amount:,.0f} 元，可用现金 {cash:,.0f} + "
            f"卖出回笼 {plan.sell_amount:,.0f} = {available:,.0f} 元，**不足**。"
            f"先卖后买，或下调 position")
    return plan


# ---------------------------------------------------------------- 输出
def render_text(plan: LivePlan, top: int = 15) -> str:
    """控制台摘要（给人看的，不是给程序读的）。"""
    lines = [
        f"信号日 {plan.as_of.date()}   总资产 {plan.equity:,.0f} 元   "
        f"仓位 {plan.position:.0%}  目标持仓 {plan.n_target} 只",
        f"可投金额 {plan.investable:,.0f} 元   "
        f"买入 {plan.buy_amount:,.0f} / 卖出 {plan.sell_amount:,.0f} 元   "
        f"订单 {len(plan.orders)} 笔",
    ]
    if plan.warnings:
        lines.append("")
        lines.append("注意：")
        lines.extend(f"  - {w}" for w in plan.warnings)
    if not plan.orders.empty:
        lines.append("")
        lines.append(f"下单清单（前 {min(top, len(plan.orders))} 笔，共 {len(plan.orders)} 笔）：")
        head = plan.orders.head(top)
        lines.append(f"  {'代码':8s}{'方向':6s}{'股数':>8s}{'参考价':>10s}{'金额':>12s}  备注")
        for row in head.itertuples(index=False):
            side = {"buy": "买入", "sell": "卖出", "hold": "无法"}.get(row.side, row.side)
            lines.append(f"  {row.symbol:8s}{side:6s}{row.shares:8d}{row.price:10.2f}"
                         f"{row.amount:12,.0f}  {row.note}")
        if len(plan.orders) > top:
            lines.append(f"  ... 其余 {len(plan.orders) - top} 笔见 orders.csv")
    else:
        lines.append("")
        lines.append("无需调仓（所有偏离都在阈值内）。")
    return "\n".join(lines)


def write_plan(plan: LivePlan, out_dir: str | Path) -> Path:
    """落盘 target.csv / orders.csv / summary.json。"""
    d = Path(out_dir) / plan.as_of.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    plan.target.to_csv(d / "target.csv", index=False, encoding="utf-8")
    plan.orders.to_csv(d / "orders.csv", index=False, encoding="utf-8")
    if not plan.holdings.empty:
        plan.holdings.to_csv(d / "holdings.csv", index=False, encoding="utf-8")
    (d / "summary.json").write_text(
        json.dumps(plan.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
    return d
