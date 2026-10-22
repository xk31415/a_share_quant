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
                    choices=["equal_weight", "momentum", "low_vol"])
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

    max_pos = args.max_positions or pf.get("max_positions", 20)
    if args.industry_neutral and args.max_positions is None:
        max_pos = max(max_pos, 300)      # 行业中性持仓数由行业数决定，别被 50 截断

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
    else:
        strategy = EqualWeightStrategy(max_positions=pf.get("max_positions", 20),
                                       seed=int(args.seed),
                                       liquidity_pct=args.liquidity,
                                       capacity_aum=args.capacity_aum)
    cost_model = CostModel(
        commission_rate=cost_cfg.get("commission_rate", 0.00025),
        commission_min=cost_cfg.get("commission_min", 5.0),
        stamp_duty_rate=cost_cfg.get("stamp_duty_rate", 0.0005),
        slippage_rate=cost_cfg.get("slippage_rate", 0.0005))
    config = BacktestConfig(
        start_date=args.start or bt.get("start_date", "2021-01-04"),
        end_date=args.end or bt.get("end_date", "2023-12-29"),
        rebalance_frequency=args.freq or bt.get("rebalance_frequency", "M"),
        initial_capital=float(args.capital or bt.get("initial_capital", 1_000_000)),
        benchmark=args.benchmark or bt.get("benchmark"),
        cash_buffer=float(pf.get("cash_buffer", 0.01)))

    engine = BacktestEngine(
        provider=provider,
        strategy=strategy,
        universe_builder=UniverseBuilder(provider, rules),
        portfolio_rules=PortfolioRules(
            max_positions=max_pos,
            max_weight_per_stock=pf.get("max_weight_per_stock", 0.10)),
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
                    "max_weight_per_stock": pf.get("max_weight_per_stock", 0.10),
                    "execution_price": config.execution_price,
                    "lot_size": mr.get("lot_size", 100)},
        benchmark=config.benchmark,
        result_path=str(out_dir),
        git_commit=git_commit(root),
        validation_warnings=list(all_warnings),
        industry_source=industry_source or "none",
        notes=("synthetic random-walk data — framework smoke test only, "
               "NOT real market data" if "synthetic" in str(data_dir).lower() else ""))
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
    print(DISCLAIMER)


if __name__ == "__main__":
    main()
