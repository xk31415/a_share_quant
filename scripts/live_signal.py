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

设计前提见 `reports/research.md` §8：选股与择时三段样本外均未通过门槛，
唯一站得住的方案是「固定样本等权 N=100~200 只 + 按仓位控制回撤」。
所以本脚本**不做任何选股判断**，只负责把仓位规则翻译成委托单。
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
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


def _compute_tradeability(bars: pd.DataFrame,
                          status: pd.DataFrame | None,
                          universe: list[str],
                          as_of: pd.Timestamp,
                          held: Iterable[str] = ()) -> tuple[set[str], list[str]]:
    """计算信号日 as_of 的 (build_plan 用的可交易集合 tradeable, 样本选股池 investable)。

    ``build_plan`` 对 ``tradeable`` 的用法有两处：已持仓不在其中 → 冻结；样本入选者不在其中
    → 目标 0 股。所以它必须同时满足"持仓能卖"和"新票能买"，按是否持有分开判：
      * ``tradeable = has_bar − suspended − (limit_down ∩ held)``：只有**已持有**的跌停股冻结
        （次日大概率卖不出，按原股数占仓位——否则规划会把"卖不掉的持仓"当成已回笼现金，
        买单超出可用资金）。未持有的跌停股可买（卖难买易），必须能分到股数，
        否则占着样本名额却 0 股。
      * ``investable = universe ∩ (has_bar − suspended)``：涨停股**照常入选**。
        历史版本把未持有的涨停股剔出样本（"不追涨停"），那是用 t 日收盘对 t+1 开盘的
        近似；对"专买涨停股"的观察池策略等于永远买不进（实测 3/3 被
        blocked:limit_up）。追不追由**下单时**的执行日闸门判
        （`gm_bridge.place-orders`，口径与 `backtest/execution.py` 一致），
        信号层只出意图。
      * ``status`` 新鲜度守卫：停牌的权威来源是 `status.parquet`（`bars.is_suspended` 恒为
        False）。``status`` 缺失/为空，或不含 `as_of` 当日记录，都显式打印 `[warn]`，不静默放行。
    """
    held_set = {str(s) for s in held}
    today = bars[pd.to_datetime(bars["trade_date"]) == as_of].copy()
    has_bar = {str(s) for s in today["symbol"]}

    suspended: set[str] = set()
    if "is_suspended" in today.columns:
        suspended |= {str(s) for s, v in zip(today["symbol"], today["is_suspended"])
                      if pd.notna(v) and bool(v)}
    if status is None or status.empty:
        print(f"[warn] 停牌数据缺失：信号日 {pd.Timestamp(as_of).date()} 前后读不到 status 记录"
              f"（落后超过回看窗口或读取失败），当日停牌过滤可能漏判！", flush=True)
    else:
        st_dates = pd.to_datetime(status["trade_date"])
        st_today = status[st_dates == as_of]
        if st_today.empty:
            max_st = pd.Timestamp(st_dates.max()).normalize()
            lag_days = (pd.Timestamp(as_of).normalize() - max_st).days
            print(f"[warn] 停牌数据落后 {lag_days} 天：status 最新日期为 {max_st.date()}，"
                  f"缺少信号日 {pd.Timestamp(as_of).date()} 的记录，当日停牌过滤可能漏判！",
                  flush=True)
        elif "is_suspended" in st_today.columns:
            suspended |= {str(s) for s, v in zip(st_today["symbol"], st_today["is_suspended"])
                          if pd.notna(v) and bool(v)}

    # 已持有的跌停股冻结：t 日收盘跌停 → t+1 大概率卖不出。用收盘价判只对
    # "要不要继续持有"这个规划问题有效，且方向保守（多持一天）；最终卖不卖得掉
    # 由执行日闸门说了算。
    limit_down_syms: set[str] = set()
    if not today.empty and {"close", "limit_down"}.issubset(today.columns):
        exempt = (today["limit_exempt"].fillna(False).astype(bool)
                  if "limit_exempt" in today.columns
                  else pd.Series(False, index=today.index))
        c = pd.to_numeric(today["close"], errors="coerce")
        ld = pd.to_numeric(today["limit_down"], errors="coerce")
        is_ld = (~exempt) & ld.notna() & c.notna() & (c <= ld + 1e-4)
        limit_down_syms = {str(s) for s in today.loc[is_ld, "symbol"]}

    tradeable = has_bar - suspended - (limit_down_syms & held_set)
    # investable 不扣冻结：已持有的跌停股如果还在样本里，不能因此被踢出样本清仓
    selectable = has_bar - suspended
    investable = [str(s) for s in universe if str(s) in selectable]
    return tradeable, investable


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
    ap.add_argument("--max-unfunded", type=float, default=0.10,
                    help="资金不足（一手超过 2 倍单只预算）的目标占比超过此值时"
                         "拒绝写委托：等权组合已名不副实，需要加资金或调大 --position")
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
    # 季度调仓月的判断要用**请求日**的月份，不是回滚后交易日的月份（R08 F-43）。
    # 实测：请求 2023-04-01（季度月）回滚到 2023-03-31（非季度月），判断直接翻转，
    # 该调仓的一天被当成"只检查"跳过；2025-11-01 则反过来，非季度月被当成季度月下单。
    quarter_month = (pd.Timestamp(args.as_of).normalize().month
                     if args.as_of else as_of.month)

    # ---- 熔断 1：数据新鲜度 ----
    stale_days = (pd.Timestamp.today().normalize() - as_of).days
    if stale_days > MAX_STALE_DAYS:
        raise SystemExit(
            f"数据最后交易日 {as_of.date()}，距今 {stale_days} 天 "
            f"（阈值 {MAX_STALE_DAYS}）。先用 update_dataset 更新数据，"
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
                      and quarter_month not in DEFAULT_REBALANCE_MONTHS)
    if check_only:
        print(f"{as_of.date()}（请求月 {quarter_month}）不是季度调仓月 "
              f"{DEFAULT_REBALANCE_MONTHS}，本月只检查不下单。"
              f"要强制调仓加 --ignore-quarter", flush=True)

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

    try:
        # 向前多读 10 天以检测 status 表是否落后于行情 as_of（A3 守卫）
        st_start = cal.dates[max(0, len(cal.dates) - 10)]
        status_recent = provider.get_status(st_start, as_of)
    except Exception:
        status_recent = None
    tradeable, investable = _compute_tradeability(bars, status_recent, universe, as_of,
                                                  held=holdings)

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
    n_funded_base = len(plan.target[plan.target["tradeable"]]) if len(plan.target) else 0
    if plan.unfunded and len(plan.unfunded) > args.max_unfunded * max(n_funded_base, 1):
        raise SystemExit(
            f"\n资金不足的目标 {len(plan.unfunded)}/{n_funded_base} 只，超过 "
            f"--max-unfunded {args.max_unfunded:.0%}，**未写委托文件**。\n"
            f"按仓位 {args.position:.0%} 买齐约需总资产 {plan.min_equity:,.0f} 元；"
            f"加资金、调大 --position，或接受偏离时调大 --max-unfunded。")
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
