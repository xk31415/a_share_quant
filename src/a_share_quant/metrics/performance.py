"""绩效指标：收益、风险、回撤、换手、持仓与分年度/月度分解。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover
    from ..backtest.engine import BacktestResult

TRADING_DAYS_PER_YEAR = 252


# ---------- 基础指标 ----------

def cumulative_return(equity: pd.Series) -> float:
    """累计收益率 = 期末权益 / 期初权益 - 1。"""
    if len(equity) < 2 or float(equity.iloc[0]) <= 0:
        return 0.0
    return float(equity.iloc[-1]) / float(equity.iloc[0]) - 1.0


def annualized_return(equity: pd.Series,
                      periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """几何年化收益率。"""
    n = len(equity) - 1
    if n < 1 or float(equity.iloc[0]) <= 0:
        return 0.0
    return float(float(equity.iloc[-1]) / float(equity.iloc[0])) ** (periods_per_year / n) - 1.0


def annualized_volatility(returns: pd.Series,
                          periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """年化波动率（日收益率样本标准差 * sqrt(252)）。"""
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    return float(r.std(ddof=1) * np.sqrt(periods_per_year))


def sharpe_ratio(returns: pd.Series, risk_free_per_year: float = 0.0,
                 periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """年化 Sharpe（默认无风险利率 0）。"""
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    excess = r - risk_free_per_year / periods_per_year
    sd = float(excess.std(ddof=1))
    if sd <= 1e-12:  # 常数收益（零波动，含浮点误差）时 Sharpe 无定义，按 0 处理
        return 0.0
    return float(excess.mean() / sd * np.sqrt(periods_per_year))


def drawdown_curve(equity: pd.Series) -> pd.Series:
    """回撤曲线：equity / 历史峰值 - 1（<= 0）。"""
    return equity / equity.cummax() - 1.0


def maximum_drawdown(equity: pd.Series) -> float:
    """最大回撤（负数，如 -0.25 表示最大亏损 25%）。"""
    dd = drawdown_curve(equity)
    return float(dd.min()) if len(dd) else 0.0


def calmar_ratio(equity: pd.Series,
                 periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Calmar = 年化收益 / |最大回撤|；无回撤时无定义（返回 NaN）。"""
    mdd = abs(maximum_drawdown(equity))
    if mdd == 0.0:
        return float("nan")
    return annualized_return(equity, periods_per_year) / mdd


def win_rate(returns: pd.Series) -> float:
    """日胜率（剔除零收益日）。"""
    r = returns.dropna()
    r = r[r != 0]
    return float((r > 0).mean()) if len(r) else 0.0


# ---------- 分解报表 ----------

def annual_returns(equity: pd.Series) -> pd.Series:
    """年度收益（index=年份）。"""
    if not isinstance(equity.index, pd.DatetimeIndex) or len(equity) < 2:
        return pd.Series(dtype=float)
    year_last = equity.groupby(equity.index.year).last()
    prev = year_last.shift(1)
    prev.iloc[0] = equity.iloc[0]
    out = year_last / prev - 1.0
    out.index = [int(i) for i in out.index]
    out.index.name = "year"
    return out


def monthly_returns(equity: pd.Series) -> pd.Series:
    """月度收益（index="YYYY-MM"）。"""
    if not isinstance(equity.index, pd.DatetimeIndex) or len(equity) < 2:
        return pd.Series(dtype=float)
    m_last = equity.groupby(equity.index.to_period("M")).last()
    prev = m_last.shift(1)
    prev.iloc[0] = equity.iloc[0]
    out = m_last / prev - 1.0
    out.index = [str(p) for p in out.index]
    out.index.name = "month"
    return out


def average_holding_period(positions: pd.DataFrame) -> float:
    """平均持仓周期（交易日，近似值）。

    近似规则：对每个 symbol 的每段连续快照视为一段持仓；
    相邻快照间隔超过 10 个自然日视为断段（容纳长假）。
    """
    if positions is None or positions.empty:
        return float("nan")
    df = positions.loc[positions["shares"] > 0, ["trade_date", "symbol"]].copy()
    if df.empty:
        return float("nan")
    df = df.sort_values(["symbol", "trade_date"])
    durations: list[float] = []
    for _, g in df.groupby("symbol"):
        dates = list(pd.DatetimeIndex(pd.to_datetime(g["trade_date"])))
        run = 1
        for a, b in zip(dates, dates[1:]):
            if (b - a) <= pd.Timedelta(days=10):
                run += 1
            else:
                durations.append(float(run))
                run = 1
        durations.append(float(run))
    return float(np.mean(durations)) if durations else float("nan")


def industry_exposure(positions: pd.DataFrame,
                      industry_map: pd.DataFrame | pd.Series) -> pd.DataFrame:
    """行业暴露：每日各行业权重合计（只统计实际持仓）。

    参数
    ----
    positions : 每日持仓快照，至少含 trade_date / symbol / weight；可选 shares。
    industry_map :
        * ``DataFrame[trade_date, symbol, industry]`` —— 行业**历史快照**（推荐）；
          按 trade_date 向后取值（merge_asof），保证 t 日只用 t 日及以前已知的分类，
          分类变更不会被未来信息污染；
        * ``Series[symbol -> industry]`` —— 静态分类，仅在确认行业无历史变更时使用。

    返回
    ----
    DataFrame：index=trade_date，columns=行业名，值为该行业当日权重合计。
    缺失分类归入 ``"UNKNOWN"`` 列，绝不静默丢弃。
    """
    if positions is None or positions.empty:
        return pd.DataFrame()
    need = {"trade_date", "symbol", "weight"}
    if not need.issubset(positions.columns):
        raise KeyError(f"positions missing columns: {sorted(need - set(positions.columns))}")

    left = positions.loc[:, ["trade_date", "symbol", "weight"]].copy()
    if "shares" in positions.columns:  # 只统计实际持仓
        left = positions.loc[positions["shares"] > 0,
                             ["trade_date", "symbol", "weight"]].copy()
    left["trade_date"] = pd.to_datetime(left["trade_date"])
    if left.empty:
        return pd.DataFrame()

    if isinstance(industry_map, pd.Series):
        left["industry"] = left["symbol"].map(industry_map)
    elif isinstance(industry_map, pd.DataFrame):
        cols = {"trade_date", "symbol", "industry"}
        if not cols.issubset(industry_map.columns):
            raise KeyError(f"industry_map missing columns: {sorted(cols - set(industry_map.columns))}")
        right = industry_map.loc[:, ["trade_date", "symbol", "industry"]].copy()
        right["trade_date"] = pd.to_datetime(right["trade_date"])
        left = left.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
        right = right.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
        left = pd.merge_asof(left, right, on="trade_date", by="symbol",
                             direction="backward", allow_exact_matches=True)
    else:
        raise TypeError("industry_map must be a DataFrame or a Series")

    left["industry"] = left["industry"].fillna("UNKNOWN").replace("", "UNKNOWN")
    out = left.pivot_table(index="trade_date", columns="industry", values="weight",
                           aggfunc="sum", fill_value=0.0)
    out.index.name = "trade_date"
    out.columns.name = "industry"
    return out.sort_index()


def cost_contribution(result: "BacktestResult") -> dict[str, float]:
    """交易成本贡献：成本金额、占比、年化拖累与单位换手成本。

    成本本身已在 ``backtest/costs.py`` 独立建模，这里只做**归因度量**：
    回答"成本吃掉了多少收益"，而不是重新计算成本。

    口径说明：``total_turnover`` 用**单边**换手（``(买入额+卖出额)/2/权益``），
    因此 ``cost_bps_per_unit_turnover`` 是"每单位单边换手对应的 bps 成本"，
    可直接与文献中的换手成本比较。旧版本用的是双边口径，数值约为现值一半。
    """
    eq = result.equity_curve
    costs = result.costs
    empty = {"total_costs": 0.0, "cost_pct_of_initial_capital": 0.0,
             "cost_pct_of_avg_equity": 0.0, "annualized_cost_drag": 0.0,
             "avg_cost_per_rebalance": 0.0, "total_turnover": 0.0,
             "cost_bps_per_unit_turnover": float("nan"), "n_cost_events": 0}
    if costs is None or len(costs) == 0 or len(eq) < 2:
        return empty

    total = float(costs.sum())
    n_periods = max(len(eq) - 1, 1)
    initial = float(eq.iloc[0])
    avg_equity = float(eq.mean())
    turnover = result.turnover
    total_turnover = float(turnover.sum()) if turnover is not None and len(turnover) else 0.0
    n_events = int((costs > 0).sum())
    return {
        "total_costs": total,
        "cost_pct_of_initial_capital": total / initial if initial > 0 else float("nan"),
        "cost_pct_of_avg_equity": total / avg_equity if avg_equity > 0 else float("nan"),
        "annualized_cost_drag": (total / initial * TRADING_DAYS_PER_YEAR / n_periods
                                 if initial > 0 else float("nan")),
        "avg_cost_per_rebalance": total / n_events if n_events else 0.0,
        "total_turnover": total_turnover,
        "cost_bps_per_unit_turnover": (total / avg_equity / total_turnover * 1e4
                                       if total_turnover > 0 and avg_equity > 0
                                       else float("nan")),
        "n_cost_events": n_events,
    }


# ---------- 暖机期 ----------
def trim_warmup(eq: pd.Series, positions: Optional[pd.DataFrame] = None) -> tuple[pd.Series, int]:
    """截掉开头"策略还不可能持仓"的那一段，返回 (截后净值, 被截掉的净值点数)。

    为什么必须截：回测起点由**数据起点**决定，而 ``min_list_days=60`` 要求
    上市满 60 个交易日才入池。数据从 2015-01-05 开始时，前 60 个交易日
    股票池为空，净值恒等于初始资金。这段被计入年化的分母（约 4 个月），
    而基准在这段时间是活的——2015-01-05~2015-05-04 中证1000 涨了 **69.8%**，
    策略却记为 0 收益。不截掉，超额会被系统性压低，样本外结论直接失真。

    **切点必须落在首次持仓日的"前一个净值点"**：首次持仓当天的收益是策略的
    第一笔盈亏，要成为 ``pct_change`` 的第一个值。切在首次持仓日当天，等于把
    建仓日的涨跌从年化/波动/夏普/超额里删掉（2026-10-21 的 R05 审查 F-16）。
    实测三段建仓日分别涨 +2.04%/+1.30%/+1.92%，年化因此低估 0.47/0.27/1.39pp；
    而 ``cumulative_return`` 用的是全序列、把这一天算进去了，两个指标会自相矛盾。

    优先用持仓表判断（第一次有持仓那天之前都是暖机）；没有持仓表时
    退回"净值首次偏离初始值"。
    """
    if eq is None or len(eq) == 0:
        return eq, 0

    start = None
    if positions is not None and len(positions) and "trade_date" in positions.columns:
        dates = pd.to_datetime(positions["trade_date"])
        if "shares" in positions.columns:
            dates = dates[positions["shares"].fillna(0) > 0]
        if len(dates):
            start = dates.min()
    if start is None:
        # 已知分叉（README 限制清单第 28 条 / R06 F-28）：持仓表一旦给出切点，
        # 净值是否平坦就不再参与判断。若"首次持仓日的净值仍等于初始资金"
        # （零成本 + 成交日收盘还没把净值打离初始值），这条退路会给出另一个
        # 切点——实测 warmup=1（持仓）对 warmup=5（平盘）。默认 CostModel 的
        # 最低佣金 5 元会在成交日改变净值，所以真实回测不触发。
        # 退回"开头连续等于初始值"的长度。只截长度 >=2 的平坦段：
        # 全程持仓的策略首日也等于初始值（长度 1），那不算暖机，
        # 截掉会让年化的起点错位一天。
        flat = (eq - eq.iloc[0]).abs() <= 1e-12
        n_flat = int(flat.cumprod().sum())
        if n_flat >= 2 and n_flat < len(eq):
            start = eq.index[n_flat]

    if start is None:
        return eq, 0
    start = pd.Timestamp(start)

    # 前移一个净值点：它是首个收益的基准。两条路径都适用——持仓表路径取
    # 首次持仓日的前一交易日；平盘路径取最后一段平盘里的最后一个点。
    prior = eq.index[eq.index < start]
    base = prior[-1] if len(prior) else start

    trimmed = eq[eq.index >= base]
    if len(trimmed) < 2:              # 全部在暖机内，不截（避免空序列）
        return eq, 0
    return trimmed, int(len(eq) - len(trimmed))


# ---------- 汇总 ----------


def benchmark_metrics(result: "BacktestResult",
                      ret: Optional[pd.Series] = None) -> dict[str, Any]:
    """相对基准的超额指标；没有基准数据时返回空字典。

    基准价来自 ``benchmark.csv``（或 ``bars`` 里同代码的行情），
    缺失时 ``result.benchmark is None``，这里什么都不算——不静默补 0。

    注意（R05 F-19 修复）：不再使用 ``bench.pct_change().reindex(ret.index).fillna(0.0)``。
    若基准序列内部缺日（如增量更新时 benchmark.csv 漏更一天），旧实现会把缺日填成
    0 收益、再在下一天把两天涨跌算成一天，导致日超额序列错位。
    现在仅在策略与基准**共有**的日期截面（``ret.index.intersection(bench.index)``）上
    计算逐日收益率并配对，同时显式记录 ``benchmark_missing_days``。
    """
    bench = getattr(result, "benchmark", None)
    if ret is None:
        ret = result.daily_returns
    if bench is None or len(bench) < 2 or ret is None or ret.empty:
        return {}
    bench_clean = bench.dropna().sort_index()
    # ret 的首日（基准点）为 NaN，有效收益日是 ret.dropna().index
    ret_valid = ret.dropna()
    if len(ret_valid) < 2 or len(bench_clean) < 2:
        return {}
    missing_dates = ret_valid.index.difference(bench_clean.index)
    n_missing = int(len(missing_dates))
    # 只数策略区间内基准有、策略没有的日子；区间外（预热期被 trim_warmup 截掉的部分）
    # 本来就该多出来，算进去会让这个字段正常运行时恒非零，失去告警意义。
    in_window = bench_clean.loc[ret.index.min():ret.index.max()]
    extra_dates = in_window.index.difference(ret.index)
    n_extra = int(len(extra_dates))
    # 仅在策略与基准共有的日期序列上对齐价格再算 pct_change，避免缺日错位
    common_idx = ret.index.intersection(bench_clean.index)
    if len(common_idx) < 2:
        return {}
    b = bench_clean.reindex(common_idx).pct_change()
    if float(b.std(ddof=1)) == 0.0 and float(b.abs().sum()) == 0.0:
        return {}
    # 逐日配对后再算，避免首日 NaN 把 beta 拖成 nan
    pair = pd.DataFrame({"r": ret.reindex(common_idx), "b": b}).dropna()
    if len(pair) < 2:
        return {}
    ret_p, b_p = pair["r"], pair["b"]
    excess = ret_p - b_p
    n = max(len(b_p), 1)
    eq_b = (1.0 + b_p).cumprod()
    te = float(excess.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
    ann_excess = float(excess.mean() * TRADING_DAYS_PER_YEAR)
    var_b = float(b_p.var(ddof=1))
    beta = float(np.cov(ret_p, b_p, ddof=1)[0, 1] / var_b) if var_b > 0 else float("nan")
    out: dict[str, Any] = {
        "benchmark_symbol": str(getattr(bench, "name", "") or ""),
        "benchmark_cumulative_return": float(eq_b.iloc[-1] - 1.0),
        "benchmark_annualized_return": float(eq_b.iloc[-1] ** (TRADING_DAYS_PER_YEAR / n) - 1.0),
        "benchmark_annualized_volatility": float(b_p.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)),
        "excess_cumulative_return": float((1.0 + excess).prod() - 1.0),
        "excess_annualized_return": ann_excess,
        "tracking_error": te,
        "information_ratio": float(ann_excess / te) if te > 0 else float("nan"),
        "beta": beta,
        "benchmark_missing_days": n_missing,
        "benchmark_extra_days": n_extra,
    }
    return out


def summarize(result: "BacktestResult") -> dict[str, Any]:
    """回测结果汇总（不只有一个最终收益率）。"""
    eq_full = result.equity_curve
    trades = result.trades
    positions = result.positions
    has_trades = trades is not None and not trades.empty
    has_positions = positions is not None and not positions.empty

    # 暖机期（股票池为空、净值不动）不计入年化/波动/夏普与超额，
    # 因为它不是策略选择，而是数据起点 + min_list_days 的副产物。
    eq, warmup_days = trim_warmup(eq_full, positions if has_positions else None)
    ret = eq.pct_change()

    # 首次持仓日：**不能取 eq.index[0]**——那是首个收益的基准点（首次持仓日的
    # 前一交易日），报出去会让人以为建仓早了一天。
    active_start = None
    if has_positions:
        _d = pd.to_datetime(positions["trade_date"])
        if "shares" in positions.columns:
            _d = _d[positions["shares"].fillna(0) > 0]
        if len(_d):
            active_start = pd.Timestamp(_d.min())
    if active_start is None and len(eq):
        # 已知偏差（README 限制清单第 29 条 / R06 F-29）：没有持仓表时的退路
        # 取的正是上面注释说"不能取"的 eq.index[0]——它是首个收益的基准点，
        # 净值仍等于初始资金。报出的日期会早于净值真正开始变动的那天，
        # n_cash_only_days 也跟着少算一天。引擎正常产出持仓表时不走这里。
        active_start = eq.index[0]

    summary: dict[str, Any] = {
        "start": str(eq_full.index[0].date()) if len(eq_full) else None,
        "end": str(eq_full.index[-1].date()) if len(eq_full) else None,
        "trading_days": int(len(eq_full)),
        "effective_days": int(len(eq)),
        "warmup_days": int(warmup_days),
        "first_active_date": str(active_start.date()) if active_start is not None else None,
        # 建仓前的空仓交易日数，在**全序列**上算（在截断后的序列上算恒为 0）
        "n_cash_only_days": (
            int((eq_full.index < active_start).sum())
            if active_start is not None else 0),
        "final_equity": float(eq_full.iloc[-1]) if len(eq_full) else float("nan"),
        "cumulative_return": cumulative_return(eq_full),
        "annualized_return": annualized_return(eq),
        "annualized_volatility": annualized_volatility(ret),
        "sharpe_ratio": sharpe_ratio(ret),
        "maximum_drawdown": maximum_drawdown(eq),
        "calmar_ratio": calmar_ratio(eq),
        "win_rate_daily": win_rate(ret),
        "n_trade_events": int(len(trades)) if has_trades else 0,
        "n_filled_trades": int((trades["shares"] > 0).sum()) if has_trades else 0,
        "n_blocked_trades": int(
            (~trades["note"].eq("filled")).sum()) if has_trades else 0,
        "n_capped_trades": int(trades["capped"].sum())
        if has_trades and "capped" in trades.columns else 0,
        "avg_turnover_per_rebalance": (
            float(result.turnover.mean()) if result.turnover is not None
            and len(result.turnover) else 0.0),
        "total_costs": (float(result.costs.sum()) if result.costs is not None
                        and len(result.costs) else 0.0),
        "avg_holding_period_days": average_holding_period(positions),
    }
    if has_positions:
        per_day_count = positions.groupby("trade_date")["symbol"].count()
        summary["max_positions_held"] = int(per_day_count.max())
        summary["avg_positions_held"] = float(per_day_count.mean())
        summary["max_single_weight"] = float(positions["weight"].max())
        # 注：这里曾经另给一套 `*_active` 字段（"剔除建仓前空仓期"口径）。
        # 2026-10-21 的 R05 审查 F-18 证实它算在**已经截断过**的序列上，
        # 所以激活区间口径与主口径逐位相同、n_cash_only_days 恒为 0，
        # 只剩"两套口径"这个混乱来源。已删除，需要时用 warmup_days /
        # n_cash_only_days 判断截了多少。
    else:
        summary["max_positions_held"] = 0
        summary["avg_positions_held"] = 0.0
        summary["max_single_weight"] = 0.0
    # 交易成本贡献（成本归因，避免只报告"最终收益率"）
    cc = cost_contribution(result)
    summary["cost_pct_of_initial_capital"] = cc["cost_pct_of_initial_capital"]
    summary["annualized_cost_drag"] = cc["annualized_cost_drag"]
    summary["total_turnover"] = cc["total_turnover"]
    # 相对基准的超额指标；没有基准行情时什么都不加（不静默补 0）
    summary.update(benchmark_metrics(result, ret))
    return summary


def full_report(result: "BacktestResult",
                industry_map: pd.DataFrame | pd.Series | None = None) -> dict[str, Any]:
    """汇总 + 年度/月度收益 + 回撤曲线 + 成本归因（+ 可选行业暴露）。

    键统一转字符串，便于 JSON 序列化。``industry_map`` 为 None 时不输出行业暴露
    （不会伪造行业分类）。
    """
    report = summarize(result)
    report["cost_contribution"] = cost_contribution(result)
    report["annual_returns"] = {str(k): v for k, v in
                                annual_returns(result.equity_curve).items()}
    report["monthly_returns"] = {str(k): v for k, v in
                                 monthly_returns(result.equity_curve).items()}
    report["drawdown_curve"] = {str(k): v for k, v in
                                drawdown_curve(result.equity_curve).round(6).items()}
    if industry_map is not None:
        expo = industry_exposure(result.positions, industry_map)
        if not expo.empty:
            report["industry_exposure_avg"] = {
                str(c): float(expo[c].mean()) for c in expo.columns}
            report["n_industries"] = int(expo.shape[1])
    return report


def plot_equity_curve(result: "BacktestResult", out_path: str | None = None):
    """绘制净值与回撤曲线（英文标签以避免字体缺失）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    axes[0].plot(result.equity_curve.index, result.equity_curve.values,
                 label="strategy", linewidth=1.2)
    if result.benchmark is not None:
        bench = result.benchmark / result.benchmark.iloc[0] * result.equity_curve.iloc[0]
        axes[0].plot(bench.index, bench.values, label=str(result.benchmark.name),
                     alpha=0.8, linewidth=1.0)
    axes[0].set_title("Equity Curve")
    axes[0].set_ylabel("equity (CNY)")
    axes[0].legend()
    dd = drawdown_curve(result.equity_curve)
    axes[1].fill_between(dd.index, dd.values * 100, 0, color="tab:red", alpha=0.4)
    axes[1].set_title("Drawdown (%)")
    axes[1].set_ylabel("drawdown %")
    fig.tight_layout()
    if out_path:
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path
    return fig
