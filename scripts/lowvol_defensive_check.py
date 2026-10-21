"""验证"低波当风控底仓"：回撤保护是低波效应，还是只是降了 beta？

核心问题：低波组合的 beta 只有 0.3~0.6，回撤自然更小。如果保护作用
完全能用"少持股 + 持现金"复制，那就不存在低波效应，实盘直接调仓位更省事。

做法：
1. 对每个时段，取低波组合与等权组合的净值曲线；
2. 用日收益回归出低波对等权的 beta；
3. 构造 **beta 匹配组合**：beta×等权 + (1-beta)×现金（现金按 rf 计息）；
4. 在同一 beta 下比较年化、波动、最大回撤、Sharpe、卡玛、最长水下时间。
   若低波在这四项上都优于 beta 匹配组合，才算真有风控价值。

另外给出上下行捕获率（按月度、以等权为基准）——这是"防御型"组合的标准描述。

用法：python scripts/lowvol_defensive_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"D:/a_share_quant")
sys.path.insert(0, str(ROOT / "src"))

from a_share_quant.metrics import trim_warmup  # noqa: E402
TRADING_DAYS = 252
RF_ANNUAL = 0.02          # 现金年化，保守取 2%（货币基金/逆回购）

# 每次口径变更（如 2026-10-21 的暖机切点修正）后要同步换成本批 EXP：
# 2020-2024 / 2015-2019 / 2025-2026 各一对（等权, 低波·行业中性）
PERIODS = [
    ("2020-2024", "EXP-20261021-134425-3784", "EXP-20261021-135258-4036"),
    ("2015-2019", "EXP-20261021-134607-3123", "EXP-20261021-134840-6325"),
    ("2025-2026", "EXP-20261021-134908-3218", "EXP-20261021-135345-0943"),
]


def load_eq(exp: str) -> pd.Series:
    """读净值曲线，**并截掉建仓前的空仓期**。

    不截的话这张表会用"全区间"口径：2020-2024 的等权是 9.15%，
    而主口径（`metrics.summarize`）是 9.81%——同一策略两个数，跨节对比
    就会减出假的差额（R05 F-23）。这里直接调主口径的 `trim_warmup`，
    保证这张表和 `live_readiness.md` 第 3 节用同一条规则。
    """
    d = ROOT / "results" / exp
    df = pd.read_csv(d / "equity_curve.csv", parse_dates=["trade_date"]).set_index("trade_date")
    eq = df["equity"].astype(float).sort_index()
    pos_path = d / "positions.csv"
    pos = (pd.read_csv(pos_path, dtype={"symbol": str}, parse_dates=["trade_date"])
           if pos_path.exists() else None)
    eq, _ = trim_warmup(eq, pos)
    return eq


def max_dd(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1.0).min())


def underwater_days(eq: pd.Series) -> int:
    """最长连续水下交易日数。"""
    peak = eq.cummax()
    under = eq < peak
    best = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        best = max(best, cur)
    return best


def stats(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    n = len(r)
    ann = float(eq.iloc[-1] ** (TRADING_DAYS / n) - 1)
    vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS))
    mdd = max_dd(eq)
    return {
        "ann": ann, "vol": vol, "sharpe": ann / vol if vol else float("nan"),
        "mdd": mdd, "calmar": ann / abs(mdd) if mdd else float("nan"),
        "underwater": underwater_days(eq),
        "mdd_per_vol": abs(mdd) / vol if vol else float("nan"),
    }


def capture(lv: pd.Series, base: pd.Series) -> tuple[float, float]:
    """月度上下行捕获率：基准下跌月份的收益比、上涨月份的收益比。"""
    m_lv = (1 + lv).resample("ME").prod() - 1
    m_b = (1 + base).resample("ME").prod() - 1
    j = pd.concat([m_lv.rename("lv"), m_b.rename("b")], axis=1).dropna()
    down, up = j[j["b"] < 0], j[j["b"] > 0]
    dc = down["lv"].sum() / down["b"].sum() if len(down) and down["b"].sum() != 0 else np.nan
    uc = up["lv"].sum() / up["b"].sum() if len(up) and up["b"].sum() != 0 else np.nan
    return float(uc), float(dc)


def main() -> None:
    rf_d = (1 + RF_ANNUAL) ** (1 / TRADING_DAYS) - 1
    print(f"现金假设 {RF_ANNUAL:.1%}/年；beta 用日收益对等权回归\n")

    rows = []
    for tag, ew_id, lv_id in PERIODS:
        ew, lv = load_eq(ew_id), load_eq(lv_id)
        j = pd.concat([ew.rename("ew"), lv.rename("lv")], axis=1).dropna()
        r_ew = j["ew"].pct_change().dropna()
        r_lv = j["lv"].pct_change().dropna()
        idx = r_ew.index.intersection(r_lv.index)
        r_ew, r_lv = r_ew.reindex(idx), r_lv.reindex(idx)

        beta = float(np.cov(r_lv, r_ew, ddof=1)[0, 1] / np.var(r_ew, ddof=1))
        # beta 匹配组合：beta×等权 + (1-beta)×现金
        r_blend = beta * r_ew + (1 - beta) * rf_d

        # 波动率匹配：w 使 w×等权 的波动等于低波（现金无波动）
        w_vol = float(s_lv_vol := stats(r_lv)["vol"] / stats(r_ew)["vol"])
        r_volm = w_vol * r_ew + (1 - w_vol) * rf_d

        s_lv, s_ew, s_bl = stats(r_lv), stats(r_ew), stats(r_blend)
        s_vm = stats(r_volm)
        uc, dc = capture(r_lv, r_ew)

        rows.append({
            "period": tag, "beta": beta,
            "lv": s_lv, "ew": s_ew, "blend": s_bl,
            "up_cap": uc, "down_cap": dc, "volm": s_vm, "w_vol": w_vol,
        })

        print(f"=== {tag}   低波 beta = {beta:.3f}")
        hdr = f"{'组合':16s} {'年化':>8s} {'波动':>8s} {'Sharpe':>7s} {'最大回撤':>9s} {'卡玛':>6s} {'最长水下':>8s} {'MDD/波动':>9s}"
        print(hdr)
        for name, s in (("低波·行业中性", s_lv), ("等权（基准）", s_ew),
                        (f"beta匹配({beta:.2f}股+现金)", s_bl),
                        (f"波动匹配({w_vol:.2f}股+现金)", s_vm)):
            print(f"{name:16s} {s['ann']:8.2%} {s['vol']:8.2%} {s['sharpe']:7.3f} "
                  f"{s['mdd']:9.2%} {s['calmar']:6.3f} {s['underwater']:8d} "
                  f"{s['mdd_per_vol']:9.2f}")
        print(f"上下行捕获（月度，以等权为基准）：上行 {uc:.2f} / 下行 {dc:.2f} "
              f"→ {'防御有效（下行<上行）' if dc < uc else '无防御特征'}")
        print()

    # 汇总：低波相对 beta 匹配组合的增量
    print("=== 汇总：低波 − beta匹配组合（同一 beta 下的净效果）")
    print(f"{'时段':10s} {'beta':>6s} {'年化差':>9s} {'Sharpe差':>9s} {'回撤差':>9s} {'卡玛差':>8s} {'水下差':>8s}")
    for r in rows:
        d_ann = r["lv"]["ann"] - r["blend"]["ann"]
        d_sh = r["lv"]["sharpe"] - r["blend"]["sharpe"]
        d_dd = r["lv"]["mdd"] - r["blend"]["mdd"]          # 正数=回撤更小
        d_cal = r["lv"]["calmar"] - r["blend"]["calmar"]
        d_uw = r["lv"]["underwater"] - r["blend"]["underwater"]
        print(f"{r['period']:10s} {r['beta']:6.3f} {d_ann:9.2%} {d_sh:9.3f} "
              f"{d_dd:9.2%} {d_cal:8.3f} {d_uw:8d}")

    out = ROOT / "reports" / "lowvol_defensive.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print("=== 汇总：低波 − 波动匹配组合（同一波动率下的净效果）")
    print(f"{'时段':10s} {'股仓比例':>8s} {'年化差':>9s} {'Sharpe差':>9s} {'回撤差':>9s} {'卡玛差':>8s}")
    for r in rows:
        v = r["volm"]
        print(f"{r['period']:10s} {r['w_vol']:8.2f} "
              f"{r['lv']['ann'] - v['ann']:9.2%} {r['lv']['sharpe'] - v['sharpe']:9.3f} "
              f"{r['lv']['mdd'] - v['mdd']:9.2%} {r['lv']['calmar'] - v['calmar']:8.3f}")

    print(f"\n明细已写入 {out}")


if __name__ == "__main__":
    main()
