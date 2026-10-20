"""仓位管理规则检验：什么时候该降仓位，降多少。

背景：上一轮证明"低波当风控"不成立——降风险应该直接降仓位。那就必须回答
**什么时候降**。本脚本只用指数（中证1000）的历史，构造几种常见的仓位规则，
并与"固定仓位"对照。

判据不是"收益最高"，而是：**能不能打赢同期的固定仓位**。
如果一条择时规则连"一直拿 65%"都打不过，它就是多余的复杂度。

规则（全部用月末信号，只用当日及之前的数据）：
  fixed_100 / fixed_65 / fixed_50   固定仓位对照
  ma200        指数收盘 > 200 日均线 → 100%，否则 50%
  mom12        指数过去 12 个月收益 > 0 → 100%，否则 50%
  dd10         指数距前高回撤 < 10% → 100%，否则 50%
  vol_target   目标波动 15%：w = 15% / 组合近 60 日实现波动，截断到 [0,1]

组合本体是"全宇宙等权"（与引擎同口径：剔 ST/停牌/上市<60日）。
现金按 2%/年计。

用法：python scripts/position_rule_check.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"D:/a_share_quant")
TRADING_DAYS = 252
RF_ANNUAL = 0.02
TARGET_VOL = 0.15

PERIODS = [
    ("2020-2024", ROOT / "data" / "processed"),
    ("2015-2019", ROOT / "data" / "processed_2015_2019"),
    ("2025-2026", ROOT / "data" / "processed_2025_2026"),
]


def universe_ew(d: Path, min_hist: int = 60) -> pd.Series:
    """全宇宙日频等权收益（月内买入持有，不做日频再平衡）。"""
    b = pd.read_parquet(d / "bars.parquet")
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b[(b["is_suspended"] == False) & b["adj_close"].notna()
          & (b["adj_close"] > 0) & (~b["is_st"].fillna(False))]
    b = b.sort_values(["symbol", "trade_date"])
    b["ret"] = b.groupby("symbol")["adj_close"].pct_change()
    b = b.dropna(subset=["ret"])

    px = b.pivot_table(index="trade_date", columns="symbol", values="adj_close")
    hist = px.notna().cumsum()
    ok = (hist >= min_hist).stack()
    ok.index.names = ["trade_date", "symbol"]
    keep = ok[ok].index
    b = b.set_index(["trade_date", "symbol"])
    b = b[b.index.isin(keep)].reset_index()

    return b.groupby("trade_date")["ret"].mean()


def index_series(d: Path, code: str = "000852") -> pd.Series:
    b = pd.read_csv(d / "benchmark.csv", dtype={"symbol": str})
    b["trade_date"] = pd.to_datetime(b["trade_date"].astype(str).str.slice(0, 10))
    s = b[b["symbol"] == code].set_index("trade_date")["close"].sort_index()
    return s


def month_end_weights(index: pd.Series, ew: pd.Series, rule: str) -> pd.Series:
    """每个月的目标股票仓位（用当月末可得的信息决定，下个月持有）。"""
    me = index.resample("ME").last().dropna().index
    out = {}
    fixed = {"fixed_100": 1.0, "fixed_65": 0.65, "fixed_50": 0.50}
    for ts in me:
        hist = index.loc[:ts]
        # 固定仓位规则不受暖机影响；信号规则在暖机期没有信息，先按满仓处理
        if rule in fixed:
            out[ts] = fixed[rule]
            continue
        if len(hist) < 210:
            out[ts] = 1.0
            continue
        if rule == "ma200":
            ma = hist.tail(200).mean()
            w = 1.0 if hist.iloc[-1] > ma else 0.5
        elif rule == "mom12":
            look = hist.tail(TRADING_DAYS + 1)
            r = look.iloc[-1] / look.iloc[0] - 1
            w = 1.0 if r > 0 else 0.5
        elif rule == "dd10":
            dd = hist.iloc[-1] / hist.cummax().iloc[-1] - 1
            w = 1.0 if dd > -0.10 else 0.5
        elif rule == "vol_target":
            vol = ew.loc[:ts].tail(60).std(ddof=1) * np.sqrt(TRADING_DAYS)
            w = float(np.clip(TARGET_VOL / vol, 0.0, 1.0)) if vol > 0 else 1.0
        else:
            raise ValueError(rule)
        out[ts] = w
    return pd.Series(out).sort_index()


def apply_rule(ew: pd.Series, w: pd.Series, rf_d: float) -> pd.Series:
    """按月度目标仓位合成组合日收益。"""
    wm = w.reindex(ew.index, method="ffill").fillna(w.iloc[0])
    return wm * ew + (1 - wm) * rf_d


def stats(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    n = len(r)
    ann = float(eq.iloc[-1] ** (TRADING_DAYS / n) - 1)
    vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS))
    mdd = float((eq / eq.cummax() - 1).min())
    under = (eq < eq.cummax())
    best = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        best = max(best, cur)
    return {"ann": ann, "vol": vol, "sharpe": ann / vol if vol else float("nan"),
            "mdd": mdd, "calmar": ann / abs(mdd) if mdd else float("nan"),
            "underwater": best}


RULES = ["fixed_100", "fixed_65", "fixed_50", "ma200", "mom12", "dd10", "vol_target"]


def main() -> None:
    rf_d = (1 + RF_ANNUAL) ** (1 / TRADING_DAYS) - 1
    print(f"现金 {RF_ANNUAL:.1%}/年；目标波动 {TARGET_VOL:.0%}；判据：能不能打赢固定仓位\n")

    summary = {}
    for tag, d in PERIODS:
        ew = universe_ew(d)
        idx = index_series(d)
        common = ew.index.intersection(idx.index)
        ew, idx = ew.reindex(common), idx.reindex(common)

        print(f"=== {tag}  ({len(common)} 个交易日)")
        print(f"{'规则':12s} {'年化':>8s} {'波动':>8s} {'Sharpe':>7s} {'最大回撤':>9s} {'卡玛':>6s} {'平均仓位':>8s} {'最长水下':>8s}")
        rows = {}
        for rule in RULES:
            w = month_end_weights(idx, ew, rule)
            r = apply_rule(ew, w, rf_d)
            s = stats(r)
            avg_w = float(w.reindex(ew.index, method="ffill").mean())
            rows[rule] = {**s, "avg_w": avg_w}
            print(f"{rule:12s} {s['ann']:8.2%} {s['vol']:8.2%} {s['sharpe']:7.3f} "
                  f"{s['mdd']:9.2%} {s['calmar']:6.3f} {avg_w:8.1%} {s['underwater']:8d}")
        summary[tag] = rows
        print()

    print("=== 择时规则 vs 固定 65%（同一条及格线）")
    print(f"{'规则':12s} " + " ".join(f"{t:>22s}" for t, _ in PERIODS))
    print(f"{'':12s} " + " ".join(f"{'年化差/卡玛差/回撤差':>22s}" for _ in PERIODS))
    for rule in RULES[3:]:
        cells = []
        for tag, _ in PERIODS:
            a, b = summary[tag][rule], summary[tag]["fixed_65"]
            cells.append(f"{a['ann']-b['ann']:+7.2%}/{a['calmar']-b['calmar']:+6.3f}/"
                         f"{a['mdd']-b['mdd']:+6.2%}")
        print(f"{rule:12s} " + " ".join(f"{c:>22s}" for c in cells))

    print("\n读法：择时规则要在三段都赢过固定 65% 才算有用；"
          "只在一段赢 = 那一段的市场形态恰好适合这条规则。")


if __name__ == "__main__":
    main()
