"""生成合成数据，用于框架冒烟测试。

!!! 警告 !!!
本脚本生成的是随机游走假数据（SYN 前缀、人为构造的退市/ST/停牌窗口），
仅用于验证代码链路（数据读取/股票池/调仓/T+1/成本/净值/绩效），
不代表任何真实市场，严禁用于得出研究结论。

用法：
    python scripts/generate_synthetic_data.py [--out data/interim/synthetic]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/interim/synthetic")
    ap.add_argument("--start", default="2021-01-04")
    ap.add_argument("--end", default="2023-12-29")
    ap.add_argument("--n-symbols", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cal = pd.bdate_range(args.start, args.end)
    n = args.n_symbols
    symbols = [f"SYN{i:04d}" for i in range(1, n + 1)]

    bar_rows, inst_rows, status_rows = [], [], []
    for i, sym in enumerate(symbols):
        # 上市日期：前 20 只在样本前上市，其余样本内陆续上市（检验次新过滤）
        if i < 20:
            list_date = cal[0] - pd.Timedelta(days=365)
        else:
            list_date = cal[min(len(cal) - 1, 200 + 40 * (i - 20))]
        # 2 只早上市的股票在样本中段退市（检验幸存者偏差处理）
        delist_date = cal[int(len(cal) * 0.6)] if i in (5, 12) else pd.NaT
        inst_rows.append({
            "symbol": sym, "name": f"合成股票{i + 1}",
            "list_date": list_date.date().isoformat(),
            "delist_date": delist_date.date().isoformat() if pd.notna(delist_date) else "",
        })

        active = [d for d in cal
                  if d >= list_date and (pd.isna(delist_date) or d <= delist_date)]
        if not active:
            continue
        mu, sigma = float(rng.uniform(-0.0005, 0.0008)), float(rng.uniform(0.012, 0.025))
        prev_close = None
        for d in active:
            open_px = 50.0 if prev_close is None else prev_close * float(np.exp(rng.normal(0, 0.006)))
            close = open_px * float(np.exp(rng.normal(mu, sigma)))
            high = max(open_px, close) * float(np.exp(abs(rng.normal(0, 0.004))))
            low = min(open_px, close) * float(np.exp(-abs(rng.normal(0, 0.004))))
            volume = float(rng.uniform(5e5, 2e7))
            pre_close = prev_close if prev_close is not None else open_px
            bar_rows.append({
                "trade_date": d.date().isoformat(), "symbol": sym,
                "open": round(open_px, 2), "high": round(high, 2),
                "low": round(low, 2), "close": round(close, 2),
                "volume": round(volume), "amount": round(volume * (open_px + close) / 2, 2),
                "adj_close": round(close, 4),       # 无公司行动 -> 复权价=原始价
                "pre_close": round(pre_close, 2),
                "limit_up": round(pre_close * 1.10, 2),
                "limit_down": round(pre_close * 0.90, 2),
                "is_suspended": False, "is_st": False,
            })
            prev_close = close
        # 状态表：1 只 ST 窗口、1 只停牌窗口（默认正常）
        for d in active:
            is_st = (i == n - 3 and pd.Timestamp("2022-03-01") <= d <= pd.Timestamp("2022-09-30"))
            is_susp = (i == n - 4 and pd.Timestamp("2022-06-01") <= d <= pd.Timestamp("2022-06-30"))
            status_rows.append({"trade_date": d.date().isoformat(), "symbol": sym,
                                "is_st": bool(is_st), "is_suspended": bool(is_susp)})

    pd.DataFrame(bar_rows).to_csv(out / "bars.csv", index=False)
    pd.DataFrame(inst_rows).to_csv(out / "instruments.csv", index=False)
    pd.DataFrame(status_rows).to_csv(out / "status.csv", index=False)
    pd.DataFrame({"trade_date": [d.date().isoformat() for d in cal]}
                 ).to_csv(out / "calendar.csv", index=False)

    print(f"[synthetic] {len(symbols)} symbols, {len(cal)} trading days "
          f"({cal[0].date()} ~ {cal[-1].date()}), {len(bar_rows)} bar rows")
    print(f"[synthetic] written to: {out.resolve()}")
    print("=" * 60)
    print("!!! 警告：以上为随机游走合成数据，仅用于框架冒烟测试，")
    print("!!! 不代表真实市场，严禁用于得出任何研究结论。")
    print("=" * 60)


if __name__ == "__main__":
    main()
