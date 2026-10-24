"""运行最简单的基准回测（等权 + 定期调仓），验证整条链路。

用法：
    python scripts/run_baseline_backtest.py --data-dir data/interim/synthetic \
        --start 2021-01-04 --end 2023-12-29

产出：
    results/EXP-.../  净值/成交/持仓/换手/成本/指标/图
    experiments/EXP-....json  实验元数据（参数、数据版本、Git commit）
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import yaml

from a_share_quant.backtest.costs import CostModel
from a_share_quant.backtest.engine import BacktestConfig, BacktestEngine
from a_share_quant.data.loader import LocalFileProvider
from a_share_quant.data.validation import validate_dataset
from a_share_quant.metrics.performance import (
    annual_returns,
    cost_contribution,
    full_report,
    industry_exposure,
    monthly_returns,
    plot_equity_curve,
    summarize,
)
from a_share_quant.portfolio.construction import PortfolioRules
from a_share_quant.research.experiments import (
    ExperimentRecord,
    git_commit,
    new_experiment_id,
    save_experiment,
)
from a_share_quant.strategies.equal_weight import EqualWeightStrategy
from a_share_quant.strategies.hype_watch import (
    HypeWatchParams,
    HypeWatchStrategy,
    PAPER_DISCLAIMER,
    build_observations,
    load_theme_map_csv,
    load_watchlist_csv,
    paper_summary,
    runtime_overrides,
)
from a_share_quant.strategies.low_vol import LowVolatility
from a_share_quant.strategies.momentum import TopNMomentum
from a_share_quant.universe.builder import UniverseBuilder, UniverseRules

DISCLAIMER = (
    "\n" + "=" * 64 +
    "\n声明：本次回测使用的数据与结果仅用于验证研究框架，\n"
    "回测结果不代表未来收益，本项目用于研究，不构成投资建议。\n"
    "在接入真实 A 股历史数据之前，上述数字不具备任何市场含义。\n" +
    "=" * 64
)


def _build_hype_watch(root: Path, args, settings: dict, provider, rules):
    """装配观察池仿真。失败直接退出，不把缺基准/缺题材改成空仓继续跑。"""
    try:
        params = HypeWatchParams.from_mapping(settings.get("hype_watch"))
        overrides = runtime_overrides(
            params,
            freq=args.freq,
            execution_price=args.execution_price,
            max_positions=args.max_positions,
            industry_neutral=bool(args.industry_neutral))
    except (TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    def _resolve(raw):
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else root / path

    watchlist = None
    theme_map = None
    if args.watchlist:
        try:
            watchlist = load_watchlist_csv(_resolve(args.watchlist))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"晨报 CSV 读不了：{exc}") from exc
    if args.themes:
        try:
            theme_map = load_theme_map_csv(_resolve(args.themes))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"题材成员表读不了：{exc}") from exc

    bench_code = str(args.benchmark or params.benchmark)
    try:
        benchmark = provider.get_benchmark(bench_code)
    except Exception as exc:
        raise SystemExit(f"读基准 {bench_code} 失败：{exc}") from exc
    excluded = list(rules.excluded_symbols or [])
    if "000300" not in {str(s) for s in excluded}:
        excluded.append("000300")
    try:
        strategy = HypeWatchStrategy(
            params=params,
            watchlist=watchlist,
            theme_map=theme_map,
            benchmark=benchmark,
            excluded_symbols=excluded)
    except (TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if overrides["rebalance_frequency"] != "D":
        strategy.warnings.append(
            "hype_watch: 本次调仓频率不是 D，冷静期按信号日计数，不等于交易日个数。")
    print(PAPER_DISCLAIMER)
    return strategy, params, overrides, bench_code


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/interim/synthetic")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--universe-config", default="config/universe.yaml")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--freq", default=None, choices=["D", "W", "M"])
    ap.add_argument("--out", default="results")
    ap.add_argument("--strategy", default="equal_weight",
                    choices=["equal_weight", "momentum", "low_vol", "hype_watch"])
    ap.add_argument("--watchlist", default=None,
                    help="晨报/观察池 CSV（trade_date,symbol,theme[，rank]）。"
                         "只属于 --strategy hype_watch，paper / 观察仿真")
    ap.add_argument("--themes", default=None,
                    help="题材成员 CSV（symbol,theme,start_date[,end_date]）。"
                         "只属于 --strategy hype_watch")
    ap.add_argument("--execution-price", default=None, choices=["open", "close"],
                    help="hype_watch：下一交易日撮合列。默认用 hype_watch.execution_price")
    ap.add_argument("--capital", type=float, default=None,
                    help="初始资金（默认取 settings.yaml，1,000,000）")
    ap.add_argument("--industry-neutral", action="store_true",
                    help="低波策略按行业分组选股（每行业取 k 只低波），对冲行业暴露")
    ap.add_argument("--k-per-industry", type=int, default=1,
                    help="行业中性时每个行业取几只（默认 1）")
    ap.add_argument("--max-positions", type=int, default=None,
                    help="覆盖 settings.yaml 的 max_positions（行业中性需要放宽）")
    ap.add_argument("--capacity-aum", type=float, default=None,
                    help="启用容量约束的 AUM 口径（元）；不传则不启用")
    ap.add_argument("--liquidity", type=float, default=None,
                    help="只保留成交额最高的这个比例的股票（0.33 = 前 1/3）")
    ap.add_argument("--seed", type=int, default=0, help="等权基准的抽样种子（换一批股票）")
    ap.add_argument("--window", type=int, default=None, help="动量/反转回看窗口")
    ap.add_argument("--skip", type=int, default=None, help="跳过最近 N 个交易日")
    ap.add_argument("--reverse", action="store_true",
                    help="动量策略反向：选动量最弱的一批（反转）")
    ap.add_argument("--benchmark", default=None,
                    help="覆盖配置里的基准代码（如 000300 / 000905 / 000852）")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    settings = {}
    cfg_path = root / args.config
    if cfg_path.exists():
        settings = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    bt = settings.get("backtest", {})
    pf = settings.get("portfolio", {})
    cost_cfg = settings.get("costs", {})
    mr = settings.get("market_rules", {})

    provider = LocalFileProvider(data_dir)

    # 全量数据质量校验（致命问题直接失败）。引擎启动时还会对回测窗口内的数据
    # 再校验一次（覆盖编程式调用路径），两边告警稍后合并去重打印。
    bars_all = provider.get_bars("1990-01-01", "2099-12-31")
    warnings = validate_dataset(
        bars=bars_all,
        instruments=provider.get_instruments(),
        status=provider.get_status("1990-01-01", "2099-12-31"),
        calendar=provider.get_calendar())

    uni_cfg = root / args.universe_config
    rules = (UniverseRules.from_yaml(uni_cfg) if uni_cfg.exists()
             else UniverseRules())
    ind_map = None
    if args.industry_neutral:
        inst_all = provider.get_instruments()
        if "industry" not in inst_all.columns or inst_all["industry"].isna().all():
            raise SystemExit("--industry-neutral 需要 instruments 里有 industry 列"
                             "（先跑 scripts/fetch_industry.py）")
        ind_map = dict(zip(inst_all["symbol"].astype(str), inst_all["industry"].astype(str)))
        print(f"[info] 行业中性选股：{len(ind_map)} 只标的，"
              f"{len(set(ind_map.values()))} 个行业")

    if args.strategy != "hype_watch" and (args.watchlist or args.themes or args.execution_price):
        raise SystemExit("--watchlist / --themes / --execution-price 只属于 --strategy hype_watch")
    if args.strategy == "hype_watch" and args.industry_neutral:
        raise SystemExit("hype_watch 不使用 --industry-neutral")

    max_pos = args.max_positions or pf.get("max_positions", 20)
    if args.industry_neutral and args.max_positions is None:
        max_pos = max(max_pos, 300)      # 行业中性持仓数由行业数决定，别被 50 截断
    max_weight = pf.get("max_weight_per_stock", 0.10)
    freq = args.freq or bt.get("rebalance_frequency", "M")
    exec_price = bt.get("execution_price", "open") or "open"

    if args.strategy == "low_vol":
        strategy = LowVolatility(top_n=max_pos,
                                 window=int(args.window or 60),
                                 liquidity_pct=args.liquidity,
                                 capacity_aum=args.capacity_aum,
                                 industry_map=ind_map,
                                 k_per_industry=args.k_per_industry)
    elif args.strategy == "momentum":
        # top_n 必须跟着 max_pos 走（R08 F-44）。原来写死配置里的 50，
        # 于是 `--max-positions 10` 时动量仍选 50 只，再由 build_target_weights
        # 按「权重降序、同权重字典序」截到 10 只——动量是**等权**，
        # 截断完全按代码字典序，名次信息在截断那一步丢光。
        strategy = TopNMomentum(top_n=max_pos,
                                window=int(args.window or bt.get("momentum_window", 60)),
                                skip=int(args.skip if args.skip is not None else bt.get("momentum_skip", 5)),
                                reverse=bool(args.reverse),
                                liquidity_pct=args.liquidity,
                                capacity_aum=args.capacity_aum,
                                industry_map=ind_map,
                                k_per_industry=args.k_per_industry)
    elif args.strategy == "hype_watch":
        strategy, hype_params, hype_over, hype_bench = _build_hype_watch(
            root, args, settings, provider, rules)
        freq = hype_over["rebalance_frequency"]
        exec_price = hype_over["execution_price"]
        max_pos = hype_over["max_positions"]
        max_weight = hype_over["max_weight_per_stock"]
        cost_model = hype_params.cost_model()
    else:
        strategy = EqualWeightStrategy(max_positions=pf.get("max_positions", 20),
                                       seed=int(args.seed),
                                       liquidity_pct=args.liquidity,
                                       capacity_aum=args.capacity_aum)
    if args.strategy != "hype_watch":
        hype_params = None
        hype_bench = None
        cost_model = CostModel(
            commission_rate=cost_cfg.get("commission_rate", 0.00025),
            commission_min=cost_cfg.get("commission_min", 5.0),
            stamp_duty_rate=cost_cfg.get("stamp_duty_rate", 0.0005),
            slippage_rate=cost_cfg.get("slippage_rate", 0.0005))
    config = BacktestConfig(
        start_date=args.start or bt.get("start_date", "2021-01-04"),
        end_date=args.end or bt.get("end_date", "2023-12-29"),
        rebalance_frequency=freq,
        initial_capital=float(args.capital or bt.get("initial_capital", 1_000_000)),
        benchmark=hype_bench or args.benchmark or bt.get("benchmark"),
        execution_price=exec_price,
        cash_buffer=float(pf.get("cash_buffer", 0.01)))

    engine = BacktestEngine(
        provider=provider,
        strategy=strategy,
        universe_builder=UniverseBuilder(provider, rules),
        portfolio_rules=PortfolioRules(
            max_positions=max_pos,
            max_weight_per_stock=max_weight),
        cost_model=cost_model,
        config=config,
        lot_size=mr.get("lot_size", 100),
        enforce_limit=mr.get("consider_limit_up_down", True),
        enforce_suspension=mr.get("consider_suspension", True),
        max_participation_rate=float(mr.get("max_participation_rate", 0.0)))

    result = engine.run()
    # 引擎自身也会做校验（覆盖编程式调用路径）；这里把全量数据校验的告警一并打印，
    # 去重后避免同一条 warning 出现两次。
    all_warnings = list(dict.fromkeys(list(warnings) + list(result.validation_warnings)))
    for w in all_warnings:
        print(f"[warn] {w}")
    summary = summarize(result)

    # 行业分类：优先用 bars 提供的逐日快照（无未来函数风险）；
    # 无历史快照时才退回 instruments 的静态分类，并明确告警由使用者确认，
    # 避免"用最新行业分类回溯历史"在暴露归因中引入前视偏差。
    instruments = provider.get_instruments()
    industry_map = None
    industry_source = None
    if "industry" in bars_all.columns and bars_all["industry"].notna().any():
        industry_map = bars_all.loc[:, ["trade_date", "symbol", "industry"]]
        industry_source = "bars 逐日快照（无未来函数风险）"
    elif "industry" in instruments.columns and instruments["industry"].notna().any():
        industry_map = instruments.set_index("symbol")["industry"]
        industry_source = ("instruments 静态分类 —— 若行业发生过变更，"
                           "暴露归因存在前视偏差，仅作参考")
    if industry_map is not None:
        print(f"[info] industry exposure source: {industry_source}")

    # ---- 保存结果 ----
    exp_id = new_experiment_id()
    out_dir = root / args.out / exp_id
    out_dir.mkdir(parents=True, exist_ok=True)
    result.equity_curve.rename("equity").to_csv(out_dir / "equity_curve.csv",
                                                index_label="trade_date")
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    result.positions.to_csv(out_dir / "positions.csv", index=False)
    if result.universe_violations is not None and not result.universe_violations.empty:
        result.universe_violations.to_csv(out_dir / "universe_violations.csv",
                                          index=False)
    result.turnover.rename("turnover").to_csv(out_dir / "turnover.csv",
                                              index_label="trade_date")
    result.costs.rename("costs").to_csv(out_dir / "costs.csv",
                                        index_label="trade_date")
    if industry_map is not None:
        industry_exposure(result.positions, industry_map).round(6).to_csv(
            out_dir / "industry_exposure.csv", index_label="trade_date")
    annual_returns(result.equity_curve).rename("annual_return").to_csv(
        out_dir / "annual_returns.csv", index_label="year")
    monthly_returns(result.equity_curve).rename("monthly_return").to_csv(
        out_dir / "monthly_returns.csv", index_label="month")
    report = full_report(result, industry_map=industry_map)
    # 一次实验要在磁盘上自我说明：校验告警与行业数据来源都进结果，
    # 不能只在终端闪一下（否则忽略返回值时，坏数据看起来像正常回测）。
    report["validation_warnings"] = list(all_warnings)
    report["validation_warnings_source"] = "full_sample + backtest_window（已去重）"
    report["industry_source"] = industry_source or "none"
    report["n_universe_violations"] = int(len(result.universe_violations)) \
        if result.universe_violations is not None else 0
    summary_paper = None
    if isinstance(strategy, HypeWatchStrategy):
        sig = pd.DataFrame(strategy.signal_log)
        sig.to_csv(out_dir / "signal_log.csv", index=False)
        cal = provider.get_calendar().slice(config.start_date, config.end_date)
        trade_dates = pd.to_datetime(bars_all["trade_date"])
        window_bars = bars_all.loc[
            (trade_dates >= pd.Timestamp(config.start_date))
            & (trade_dates <= pd.Timestamp(config.end_date))]
        try:
            obs = build_observations(
                sig, window_bars, strategy.params.observe_horizons, cal)
        except ValueError as exc:
            raise SystemExit(f"观察窗口算不了：{exc}") from exc
        obs.to_csv(out_dir / "observations.csv", index=False)
        summary_paper = paper_summary(result, sig, strategy.params)
        with open(out_dir / "paper_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary_paper, f, ensure_ascii=False, indent=2, default=str)
        report["paper_only"] = True
        report["disclaimer"] = PAPER_DISCLAIMER
        report["research_claim"] = summary_paper["research_claim"]
        report["hype_watch"] = summary_paper
    with open(out_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    with open(out_dir / "cost_contribution.json", "w", encoding="utf-8") as f:
        json.dump(cost_contribution(result), f, ensure_ascii=False, indent=2,
                  default=str)
    try:
        plot_equity_curve(result, str(out_dir / "equity_curve.png"))
    except Exception as exc:  # 图形环境异常不阻断回测
        print(f"[warn] plot failed: {exc}")

    record = ExperimentRecord(
        experiment_id=exp_id,
        strategy_name=strategy.name,
        data_version=provider.data_version(),
        universe=repr(rules),
        start_date=config.start_date,
        end_date=config.end_date,
        rebalance_frequency=config.rebalance_frequency,
        transaction_cost=asdict(cost_model),
        slippage=cost_model.slippage_rate,
        parameters={"max_positions": getattr(strategy, "max_positions",
                                              getattr(strategy, "top_n", None)),
                    "strategy": getattr(strategy, "name", type(strategy).__name__),
                    "window": getattr(strategy, "window", None),
                    "skip": getattr(strategy, "skip", None),
                    "reverse": getattr(strategy, "reverse", None),
                    "seed": getattr(strategy, "seed", None),
                    "liquidity_pct": getattr(strategy, "liquidity_pct", None),
                    "capacity_aum": getattr(strategy, "capacity_aum", None),
                    "initial_capital": config.initial_capital,
                    "industry_neutral": bool(args.industry_neutral),
                    "k_per_industry": args.k_per_industry if args.industry_neutral else None,
                    "max_weight_per_stock": max_weight,
                    "execution_price": config.execution_price,
                    "lot_size": mr.get("lot_size", 100),
                    "paper_only": True if hype_params is not None else None,
                    "pool_size": None if hype_params is None else hype_params.pool_size,
                    "max_themes": None if hype_params is None else hype_params.max_themes,
                    "min_limitup_count": None if hype_params is None else hype_params.min_limitup_count,
                    "min_theme_excess": None if hype_params is None else hype_params.min_theme_excess,
                    "position_mode": None if hype_params is None else hype_params.position_mode,
                    "gross_exposure": None if hype_params is None else hype_params.gross_exposure},
        benchmark=config.benchmark,
        result_path=str(out_dir),
        git_commit=git_commit(root),
        validation_warnings=list(all_warnings),
        industry_source=industry_source or "none",
        notes=(
            PAPER_DISCLAIMER if hype_params is not None else
            ("synthetic random-walk data — framework smoke test only, "
             "NOT real market data" if "synthetic" in str(data_dir).lower() else "")))
    save_experiment(record, root / "experiments")

    # ---- 汇报 ----
    print(f"\n[experiment] {exp_id}")
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"  {k:32s} {v:,.6f}")
        else:
            print(f"  {k:32s} {v}")
    cc = cost_contribution(result)
    print("\n[cost contribution]")
    print(f"  {'cost_pct_of_initial_capital':32s} {cc['cost_pct_of_initial_capital']:,.6%}")
    print(f"  {'annualized_cost_drag':32s} {cc['annualized_cost_drag']:,.6%}")
    print(f"  {'cost_bps_per_unit_turnover':32s} {cc['cost_bps_per_unit_turnover']:,.4f}")
    if industry_map is not None:
        expo = industry_exposure(result.positions, industry_map)
        print("\n[industry exposure — 期间平均权重]")
        for col in expo.columns:
            print(f"  {col:32s} {expo[col].mean():,.6f}")
    print(f"\n[result files] {out_dir}")
    if summary_paper is not None:
        print("\n[paper / 观察仿真]")
        print(f"  {'final_equity':32s} {summary_paper['final_equity']:,.6f}")
        print(f"  {'maximum_drawdown':32s} {summary_paper['maximum_drawdown']:,.6f}")
        print(f"  {'n_rebalances':32s} {summary_paper['n_rebalances']}")
        print(f"  {'total_costs':32s} {summary_paper['total_costs']:,.6f}")
        print(f"  {'one_way_friction':32s} {summary_paper['one_way_friction']:.6f}")
        print("  以上数字只描述这次纸面路径，不是研究结论。")
        print(PAPER_DISCLAIMER)
    print(DISCLAIMER)


if __name__ == "__main__":
    main()
