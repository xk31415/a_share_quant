"""绩效指标：收益、风险、回撤、换手、持仓与分年度/月度分解。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


# ---------- 汇总 ----------

def summarize(result: "BacktestResult") -> dict[str, Any]:
    """回测结果汇总（不只有一个最终收益率）。"""
    eq, ret = result.equity_curve, result.daily_returns
    trades = result.trades
    positions = result.positions
    has_trades = trades is not None and not trades.empty
    has_positions = positions is not None and not positions.empty
    summary: dict[str, Any] = {
        "start": str(eq.index[0].date()) if len(eq) else None,
        "end": str(eq.index[-1].date()) if len(eq) else None,
        "trading_days": int(len(eq)),
        "final_equity": float(eq.iloc[-1]) if len(eq) else float("nan"),
        "cumulative_return": cumulative_return(eq),
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
    else:
        summary["max_positions_held"] = 0
        summary["avg_positions_held"] = 0.0
        summary["max_single_weight"] = 0.0
    return summary


def full_report(result: "BacktestResult") -> dict[str, Any]:
    """汇总 + 年度/月度收益 + 回撤曲线（键统一转字符串，便于 JSON 序列化）。"""
    report = summarize(result)
    report["annual_returns"] = {str(k): v for k, v in
                                annual_returns(result.equity_curve).items()}
    report["monthly_returns"] = {str(k): v for k, v in
                                 monthly_returns(result.equity_curve).items()}
    report["drawdown_curve"] = {str(k): v for k, v in
                                drawdown_curve(result.equity_curve).round(6).items()}
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
