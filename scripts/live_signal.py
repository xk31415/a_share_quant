"""实盘信号：给定截至当日的数据，输出目标持仓与**需要动的单**。

用法
----
    # 1) 第一次：冻结样本（只做一次，之后永不重抽）
    python scripts/live_signal.py --data-dir data/processed_2025_2026 \
        --create-sample --n 100 --pool 1000

    # 2) 每次调仓日（默认季度：1/4/7/10 月）收盘后跑
    python scripts/live_signal.py --data-dir data/processed_2025_2026 \
        --position 0.40 --holdings live/holdings.csv

    # 3) 不出单，只看偏离
    python scripts/live_signal.py --data-dir data/processed_2025_2026 \
        --position 0.40 --holdings live/holdings.csv --dry-run

产出
----
    live/<as_of>/target.csv   目标持仓（含当前股数与是否可交易）
    live/<as_of>/orders.csv   下单清单（只含需要动的）
    live/<as_of>/summary.json 现金/仓位/告警
    没有调仓信号时只打印提示，不写文件。

设计前提见 `reports/live_readiness.md`：选股与择时三段样本外均未通过门槛，
唯一站得住的方案是「固定样本等权 N=100~200 只 + 按仓位控制回撤」。
所以本脚本**不做任何选股判断**，只负责把仓位规则翻译成委托单。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

from a_share_quant.data.loader import LocalFileProvider
from a_share_quant.live.signal import (
    quote_symbols,
    DEFAULT_LOT_SIZE,
    DEFAULT_MIN_TRADE_VALUE,
    Sample,
    build_plan,
    rank_universe,
    render_text,
    write_plan,
)
from a_share_quant.universe.builder import UniverseBuilder, UniverseRules

# 数据过期阈值：超过这么多自然日没有新行情，拒绝出信号。
# 静默用旧数据出单是实盘最危险的一类错误——参数没错、代码没错，
# 但信号来自一个月前的价格。
MAX_STALE_DAYS = 15

# 季度调仓月份：1/4/7/10。其余月份只做权重检查，不强制回到目标。
DEFAULT_REBALANCE_MONTHS = (1, 4, 7, 10)


def _load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _resolve_as_of(cal, requested: str | None) -> pd.Timestamp:
    dates = cal.dates
    if requested:
        ts = pd.Timestamp(requested).normalize()
        if ts in cal:
            return ts
        earlier = dates[dates <= ts]
        if len(earlier) == 0:
            raise SystemExit(f"--as-of {requested} 早于数据起点 {dates[0].date()}")
        print(f"[warn] {requested} 不是交易日，改用 {earlier[-1].date()}", flush=True)
        return earlier[-1]
    return dates[-1]


def _load_holdings(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"持仓文件不存在：{p}")
    df = pd.read_csv(p, dtype={"symbol": str})
    missing = {"symbol", "shares"} - set(df.columns)
    if missing:
        raise SystemExit(f"持仓文件缺列：{sorted(missing)}（需要 symbol,shares）")
    out: dict[str, int] = {}
    for sym, sh in zip(df["symbol"], pd.to_numeric(df["shares"], errors="coerce")):
        if pd.isna(sh) or int(sh) <= 0:
            continue
        out[str(sym).strip()] = int(sh)
    return out


def _latest_prices(bars: pd.DataFrame, symbols: set[str],
                   as_of: pd.Timestamp) -> dict[str, float]:
    """每个代码在 as_of 及之前的最后一个收盘价。

    停牌股用最后收盘价（而不是丢弃）：否则总资产会被低估，
    目标金额算小，卖出后又发现钱多出来。
    """
    sub = bars[(bars["trade_date"] <= as_of) & bars["symbol"].isin(symbols)]
    if sub.empty:
        return {}
    sub = sub.sort_values(["symbol", "trade_date"])
    last = sub.groupby("symbol").tail(1)
    out = {}
    for sym, px in zip(last["symbol"], last["close"]):
        if pd.notna(px) and float(px) > 0:
            out[str(sym)] = float(px)
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/processed_2025_2026")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--universe-config", default="config/universe.yaml")
    ap.add_argument("--sample", default="live/sample.json",
                    help="冻结样本文件（不存在时需先 --create-sample）")
    ap.add_argument("--create-sample", action="store_true",
                    help="按当前股票池冻结样本；已存在时需 --force")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的样本文件")
    ap.add_argument("--pool", type=int, default=1000,
                    help="冻结时的候选池大小（要远大于 n，留出补位余量）")
    ap.add_argument("--n", type=int, default=None,
                    help="目标持仓数（默认取 settings.yaml 的 max_positions）")
    ap.add_argument("--seed", type=int, default=0, help="散列种子（与回测对齐用）")
    ap.add_argument("--capital", type=float, default=None,
                    help="账户总资产（元）。给了 --cash 时以「现金 + 持仓市值」为准")
    ap.add_argument("--cash", type=float, default=None,
                    help="账户可用现金（元）。**推荐用这个**：券商里能直接看到，"
                         "总资产由它加持仓市值算出，不必手算")
    ap.add_argument("--position", type=float, default=1.0,
                    help="目标仓位 w（0~1）。回撤≈w×满仓回撤；保守建议 0.4~0.5")
    ap.add_argument("--holdings", default=None, help="当前持仓 CSV（symbol,shares）")
    ap.add_argument("--as-of", default=None, help="信号日（默认数据最后一个交易日）")
    ap.add_argument("--out-dir", default="live", help="输出根目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--ignore-quarter", action="store_true",
                    help="非季度调仓月也强制回到目标（默认只在 1/4/7/10 月调）")
    ap.add_argument("--min-trade-value", type=float,
                    default=DEFAULT_MIN_TRADE_VALUE,
                    help="单笔低于此金额不下单（省最低佣金）")
    args = ap.parse_args()

    settings = _load_config(str(root / args.config))
    bt = settings.get("backtest", {}) or {}
    pf = settings.get("portfolio", {}) or {}
    costs = settings.get("costs", {}) or {}
    market = settings.get("market_rules", {}) or {}

    n = int(args.n or pf.get("max_positions", 100))
    lot_size = int(market.get("lot_size", DEFAULT_LOT_SIZE))
    cash_buffer = float(pf.get("cash_buffer", 0.01))

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    if not data_dir.exists():
        raise SystemExit(f"数据目录不存在：{data_dir}")

    provider = LocalFileProvider(data_dir)
    cal = provider.get_calendar()
    as_of = _resolve_as_of(cal, args.as_of)

    # ---- 熔断 1：数据新鲜度 ----
    stale_days = (pd.Timestamp.today().normalize() - as_of).days
    if stale_days > MAX_STALE_DAYS:
        raise SystemExit(
            f"数据最后交易日 {as_of.date()}，距今 {stale_days} 天 "
            f"（阈值 {MAX_STALE_DAYS}）。先用 build_dataset 更新数据，"
            f"不要用过期行情出单。")

    rules = UniverseRules.from_yaml(root / args.universe_config) \
        if (root / args.universe_config).exists() else UniverseRules()
    builder = UniverseBuilder(provider, rules)
    universe = builder.get_universe(as_of)
    if len(universe) < 200:
        raise SystemExit(f"股票池只有 {len(universe)} 只，明显异常，拒绝出信号")
    print(f"数据 {data_dir.name}  信号日 {as_of.date()}  "
          f"股票池 {len(universe)} 只", flush=True)

    sample_path = Path(args.sample)
    if not sample_path.is_absolute():
        sample_path = root / sample_path

    if args.create_sample:
        if sample_path.exists() and not args.force:
            raise SystemExit(f"样本文件已存在：{sample_path}（要覆盖请加 --force）")
        pool = max(args.pool, n)
        ranked = rank_universe(universe, args.seed)[:pool]
        sample = Sample(ranked=ranked, n=n, seed=args.seed,
                        created=str(as_of.date()), data_dir=data_dir.name,
                        as_of=str(as_of.date()),
                        note="实盘冻结样本：新上市股票永不进入，退市/ST 按名次补位")
        p = sample.save(sample_path)
        print(f"已冻结样本：{p}（池 {len(ranked)} 只，取前 {n} 只）", flush=True)
        print("注意：这个文件之后不要再改。换了它等于换了一套组合，"
              "历史回测结果无法对应。", flush=True)
        return

    if not sample_path.exists():
        raise SystemExit(
            f"找不到样本文件 {sample_path}。首次实盘请先跑：\n"
            f"  python scripts/live_signal.py --data-dir {args.data_dir} "
            f"--create-sample --n {n} --pool {args.pool}")
    sample = Sample.load(sample_path)
    if sample.n != n:
        print(f"[warn] 样本文件 n={sample.n}，命令行 --n={n}，以样本文件为准",
              flush=True)
        n = sample.n
    if sample.data_dir and sample.data_dir != data_dir.name:
        print(f"[warn] 样本是在 {sample.data_dir} 上冻结的，当前用 {data_dir.name}；"
              f"成分变动会让补位行为不同", flush=True)

    # ---- 取价与可交易集合 ----
    start = cal.dates[max(0, len(cal.dates) - 90)]
    bars = provider.get_bars(start, as_of)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    holdings = _load_holdings(args.holdings)
    # 覆盖完整 ranked：补位可能越过 3n/300 名，缺价会让整张计划抛错（R05 F-21）
    need = quote_symbols(sample, holdings)
    prices = _latest_prices(bars, need, as_of)

    # ---- 熔断 2：非季度调仓月（空仓建仓不受限制，否则永远建不了仓）----
    # 只**不下单**，但"检查"必须真的做：原来这里的 return 在 build_plan 之前，
    # 打印的"本月只检查不下单"其实一笔都没查过（R06 F-30）。
    check_only = bool(not args.ignore_quarter and holdings
                      and as_of.month not in DEFAULT_REBALANCE_MONTHS)
    if check_only:
        print(f"{as_of.date()} 不是季度调仓月 {DEFAULT_REBALANCE_MONTHS}，"
              f"本月只检查不下单。要强制调仓加 --ignore-quarter", flush=True)

    # ---- 总资产：优先用「现金 + 持仓市值」----
    held_value = sum(sh * prices[s] for s, sh in holdings.items() if s in prices)
    unpriced = [s for s in holdings if s not in prices]
    if unpriced:
        raise SystemExit(f"持仓 {unpriced[:5]} 在数据里找不到价格，无法估值总资产。"
                         f"请确认数据覆盖到这些标的的最后交易日。")
    if args.cash is not None:
        capital = float(args.cash) + held_value
        print(f"账户：现金 {args.cash:,.0f} + 持仓市值 {held_value:,.0f} "
              f"= 总资产 {capital:,.0f} 元", flush=True)
    else:
        capital = float(args.capital or bt.get("initial_capital", 1_000_000))
        if holdings:
            print(f"[warn] 用 --capital {capital:,.0f} 作为总资产；"
                  f"更可靠的是 --cash（现金余额），持仓市值由程序算", flush=True)

    today = bars[bars["trade_date"] == as_of]
    suspended = set()
    if "is_suspended" in today.columns:
        suspended = {str(s) for s, v in zip(today["symbol"], today["is_suspended"])
                     if bool(v)}
    has_bar = {str(s) for s in today["symbol"]}
    tradeable = has_bar - suspended
    # 可投资池 = 股票池 ∩ 当日有成交，再按样本名次取人
    investable = [s for s in universe if s in tradeable]

    plan = build_plan(
        as_of=as_of, prices=prices, sample=sample,
        investable_universe=investable, current_holdings=holdings,
        capital=capital, position=args.position, cash_buffer=cash_buffer,
        lot_size=lot_size, min_trade_value=args.min_trade_value,
        n=n, tradeable=tradeable)

    # 成本估算：让"这一笔划不划算"可判断
    cr = float(costs.get("commission_rate", 0.00025))
    cmin = float(costs.get("commission_min", 5.0))
    stamp = float(costs.get("stamp_duty_rate", 0.0005))
    slippage = float(costs.get("slippage_rate", 0.0005))
    if not plan.orders.empty:
        buys = plan.orders[plan.orders["side"] == "buy"]
        sells = plan.orders[plan.orders["side"] == "sell"]
        cost = 0.0
        if len(buys):
            cost += sum(max(a * cr, cmin) for a in buys["amount"])
        if len(sells):
            cost += sum(max(a * cr, cmin) + a * stamp for a in sells["amount"])
        cost += (plan.buy_amount + plan.sell_amount) * slippage
        plan.warnings.append(
            f"预估成本 {cost:,.0f} 元（占调仓金额 {cost / max(plan.buy_amount + plan.sell_amount, 1):.2%}），"
            f"其中滑点按 {slippage:.2%}、佣金 {cr:.3%}（最低 {cmin:.0f} 元）、"
            f"印花税 {stamp:.2%}（仅卖出）")

    print(render_text(plan), flush=True)
    if check_only:
        print("\n非季度调仓月：以上只是**检查**，未写委托文件。"
              "确实要按这些单子调仓，加 --ignore-quarter 重跑。", flush=True)
        return
    if args.dry_run:
        print("\n--dry-run：未写文件。", flush=True)
        return
    out = write_plan(plan, root / args.out_dir)
    print(f"\n已写入 {out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
