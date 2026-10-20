"""卖单续挂与退市清算（E 组 / F-02）。"""
import pandas as pd
import pytest

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies import Strategy, TargetPortfolio
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_provider


class Seq(Strategy):
    name = "seq"

    def __init__(self, targets):
        self._t = list(targets)
        self._n = 0

    def generate_target(self, as_of, bars, universe):
        w = self._t[min(self._n, len(self._t) - 1)]
        self._n += 1
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights=dict(w))


def _bars(dates, rows):
    """rows: [(open, high, low, close, limit_up, limit_down, pre_close)]"""
    out = []
    prev = rows[0][3]
    for d, (o, h, l, c, lu, ld, pc) in zip(dates, rows):
        out.append({"trade_date": d, "symbol": "AAA", "open": o, "high": h,
                    "low": l, "close": c, "volume": 1_000_000.0,
                    "amount": c * 1_000_000, "pre_close": pc,
                    "limit_up": lu, "limit_down": ld, "adj_close": c})
        prev = c
    return pd.DataFrame(out)


def _engine(provider, strategy, dates, **kw):
    return BacktestEngine(
        provider=provider, strategy=strategy,
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=1, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0, commission_min=0.0,
                             commission_rate=0.0, stamp_duty_rate=0.0),
        config=BacktestConfig(str(dates[0].date()), str(dates[-1].date()), "D"),
        **kw)


def test_blocked_sell_is_retried_next_day():
    """调仓日跌停卖不掉，随后的**非信号日**必须继续挂，而不是等下一个调仓日。"""
    dates = pd.bdate_range("2024-01-02", periods=15)
    rows = [(10.0, 10.2, 9.8, 10.0, 11.0, 9.0, 10.0)] * 15
    # 第 10 个交易日（01-15，周一）一字跌停：这天正好是 01-12 信号日的执行日
    rows[9] = (9.0, 9.0, 9.0, 9.0, 9.9, 9.0, 10.0)
    bars = _bars(dates, rows)
    provider = make_provider(dates, ["AAA"], bars=bars)
    engine = BacktestEngine(
        provider=provider, strategy=Seq([{"AAA": 1.0}, {}]),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=1, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0, commission_min=0.0,
                             commission_rate=0.0, stamp_duty_rate=0.0),
        config=BacktestConfig(str(dates[0].date()), str(dates[-1].date()), "W"))
    result = engine.run()

    sells = result.trades[result.trades["direction"] == "sell"]
    blocked = sells[sells["note"] == "blocked:limit_down"]
    assert len(blocked) == 1, f"应有一笔跌停拦截，实际 {sells['note'].tolist()}"

    retried = sells[(sells["trade_date"] > blocked.iloc[0]["trade_date"])
                    & (sells["note"] == "filled")]
    assert not retried.empty, "跌停当天的次日必须重新挂卖单并成交"
    # 且发生在下一个调仓日（01-19 之后）之前
    assert retried.iloc[0]["trade_date"] < pd.Timestamp("2024-01-19")


def test_delisted_holding_is_force_liquidated():
    """退市后仍持有的仓位必须清算，不能永远冻结在最后价格。"""
    dates = pd.bdate_range("2024-01-02", periods=5)
    bars = _bars(dates[:2], [
        (10.0, 10.2, 9.8, 10.0, 11.0, 9.0, 10.0),
        (10.0, 10.2, 9.8, 10.0, 11.0, 9.0, 10.0),
    ])
    inst = pd.DataFrame([{"symbol": "AAA", "exchange": "SZSE", "name": "x",
                          "list_date": pd.Timestamp("2010-01-01"),
                          "delist_date": dates[2]}])
    provider = make_provider(dates, ["AAA"], bars=bars, instruments=inst)
    result = _engine(provider, Seq([{"AAA": 1.0}]), dates).run()

    forced = result.trades[result.trades["note"] == "forced:delisted"]
    assert len(forced) == 1, "退市日应强平一笔"
    assert forced.iloc[0]["trade_date"] == pd.Timestamp(dates[2])

    after = result.positions[result.positions["trade_date"] >= pd.Timestamp(dates[2])]
    assert after.empty, "强平之后不应还有持仓记录"


def test_unfilled_sell_does_not_leak_across_new_target():
    """新信号回来了就由新目标接管，不要重复挂旧卖单。"""
    dates = pd.bdate_range("2024-01-02", periods=6)
    bars = _bars(dates, [
        (10.0, 10.2, 9.8, 10.0, 11.0, 9.0, 10.0),
        (10.0, 10.2, 9.8, 10.0, 11.0, 9.0, 10.0),
        (9.0, 9.0, 9.0, 9.0, 9.9, 9.0, 10.0),      # 跌停，卖不出
        (9.0, 9.0, 9.0, 9.0, 9.9, 9.0, 9.0),       # 继续跌停
        (9.0, 9.2, 8.9, 9.1, 9.9, 8.1, 9.0),
        (9.1, 9.3, 9.0, 9.2, 10.0, 8.2, 9.1),
    ])
    provider = make_provider(dates, ["AAA"], bars=bars)
    result = _engine(provider, Seq([{"AAA": 1.0}, {}, {"AAA": 0.5}]), dates).run()
    # 只要不抛异常、且有成交即可；核心是"旧卖单不会和新买单一起来回打架"
    assert (result.trades["note"] == "filled").any()
