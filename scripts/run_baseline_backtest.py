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
    full_report,
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

    # 数据质量校验（致命问题直接失败）
    bars_all = provider.get_bars("1990-01-01", "2099-12-31")
    warnings = validate_dataset(
        bars=bars_all,
        instruments=provider.get_instruments(),
        status=provider.get_status("1990-01-01", "2099-12-31"),
        calendar=provider.get_calendar())
    for w in warnings:
        print(f"[warn] {w}")

    uni_cfg = root / args.universe_config
    rules = (UniverseRules.from_yaml(uni_cfg) if uni_cfg.exists()
             else UniverseRules())
    strategy = EqualWeightStrategy(max_positions=pf.get("max_positions", 20))
    cost_model = CostModel(
        commission_rate=cost_cfg.get("commission_rate", 0.00025),
        commission_min=cost_cfg.get("commission_min", 5.0),
        stamp_duty_rate=cost_cfg.get("stamp_duty_rate", 0.0005),
        slippage_rate=cost_cfg.get("slippage_rate", 0.0005))
    config = BacktestConfig(
        start_date=args.start or bt.get("start_date", "2021-01-04"),
        end_date=args.end or bt.get("end_date", "2023-12-29"),
        rebalance_frequency=args.freq or bt.get("rebalance_frequency", "M"),
        initial_capital=float(bt.get("initial_capital", 1_000_000)),
        benchmark=bt.get("benchmark"))

    engine = BacktestEngine(
        provider=provider,
        strategy=strategy,
        universe_builder=UniverseBuilder(provider, rules),
        portfolio_rules=PortfolioRules(
            max_positions=pf.get("max_positions", 20),
            max_weight_per_stock=pf.get("max_weight_per_stock", 0.10)),
        cost_model=cost_model,
        config=config,
        lot_size=mr.get("lot_size", 100),
        enforce_limit=mr.get("consider_limit_up_down", True),
        enforce_suspension=mr.get("consider_suspension", True))

    result = engine.run()
    summary = summarize(result)

    # ---- 保存结果 ----
    exp_id = new_experiment_id()
    out_dir = root / args.out / exp_id
    out_dir.mkdir(parents=True, exist_ok=True)
    result.equity_curve.rename("equity").to_csv(out_dir / "equity_curve.csv",
                                                index_label="trade_date")
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    result.positions.to_csv(out_dir / "positions.csv", index=False)
    result.turnover.rename("turnover").to_csv(out_dir / "turnover.csv",
                                              index_label="trade_date")
    result.costs.rename("costs").to_csv(out_dir / "costs.csv",
                                        index_label="trade_date")
    annual_returns(result.equity_curve).rename("annual_return").to_csv(
        out_dir / "annual_returns.csv", index_label="year")
    monthly_returns(result.equity_curve).rename("monthly_return").to_csv(
        out_dir / "monthly_returns.csv", index_label="month")
    with open(out_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(full_report(result), f, ensure_ascii=False, indent=2,
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
        parameters={"max_positions": strategy.max_positions,
                    "max_weight_per_stock": pf.get("max_weight_per_stock", 0.10),
                    "execution_price": config.execution_price,
                    "lot_size": mr.get("lot_size", 100)},
        benchmark=config.benchmark,
        result_path=str(out_dir),
        git_commit=git_commit(root),
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
    print(f"\n[result files] {out_dir}")
    print(DISCLAIMER)


if __name__ == "__main__":
    main()
