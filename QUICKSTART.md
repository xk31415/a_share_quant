# 操作手册

> 所有命令都在本机实际跑过。命令在项目根目录 `D:\a_share_quant` 下执行。
> PowerShell 里用 `;` 连接命令；逗号分隔的代码列表要加引号（`--limit-symbols "002168,600000"`），
> 否则会被当成数组并吃掉前导零。
>
> **回测结果不代表未来收益。本项目用于研究，不构成投资建议。**

---

## 1. 环境

| 用途 | 解释器 |
|---|---|
| 测试、回测、实盘信号、对账 | `C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（已 `pip install -e ".[dev]"`） |
| 拉行情（装了掘金 gm，需环境变量 `GM_TOKEN`） | `C:\Program Files\Python314\python.exe` |

下文的 `python` 指第一个解释器。换机器：`pip install -e ".[dev]"`，拉数据的环境另装 `gm` 与 `pyarrow`。

健康检查：

```bash
python -m pytest                        # 417 passed（不读真实数据）
python scripts/data_manifest.py verify  # 三份数据集各一行 [ok]；2025-2026 每日更新后会显示不一致，属正常
python scripts/run_baseline_backtest.py --data-dir data/processed_2025_2026 \
    --start 2025-01-01 --end 2026-10-20 --benchmark 000852
# 期望 annualized_return = 0.0610828468971103
```

---

## 2. 回测与研究

```bash
# 等权基准（散列抽样 50 只）——这是"等权口径"，不是策略
python scripts/run_baseline_backtest.py --data-dir data/processed \
    --start 2020-01-01 --end 2024-12-31 --benchmark 000852

# 低波·行业中性（持仓数由行业数决定，约 79 只）
... --strategy low_vol --industry-neutral
# 60 日反转 / 12-1 动量
... --strategy momentum --window 60 --skip 0 --reverse
... --strategy momentum --window 252 --skip 21
# 热点+龙头（日频按需；研究规则，没有收益结论；缺行业则空仓并告警）
... --strategy hotspot

# 炒作苗头观察池：paper / 观察仿真，不是买卖建议。
# 回测不写下单文件。仿真账户由 live.yaml 的 strategy=hype_watch 走 live_signal_hype.py。
# 参数与 CSV 列见 reports/hype_watch.md。没有已验证收益，不要把这次的净值写进研究结论。
python scripts/run_baseline_backtest.py --strategy hype_watch \
    --data-dir data/processed_2025_2026 --start 2025-01-01 --end 2026-10-20 \
    --themes path/to/themes.csv
# 回放晨报名单（默认仍要过涨停家数和超额门槛；只回放名单时把 csv_require_trigger 设为 false）
python scripts/run_baseline_backtest.py --strategy hype_watch \
    --data-dir data/processed_2025_2026 --themes path/to/themes.csv \
    --watchlist path/to/morning.csv

# 容量压力测试：放大本金并在选股环节启用容量约束
... --capital 500000000 --capacity-aum 500000000

# 因子研究：IC、十分档、流动性三档内收益、行业中性 IC
python scripts/factor_research.py --data-dir data/processed
# 行业归因、实验对比、全宇宙等权参照、低波 beta 匹配、择时规则、抽样跟踪误差
python scripts/industry_attribution.py results/EXP-xxxx
python scripts/compare_runs.py --last 6
python scripts/replicate_universe.py
python scripts/lowvol_defensive_check.py
python scripts/position_rule_check.py
```

产物在 `results/EXP-<时间戳>/`（先看 `equity_curve.png` 与 `metrics.json`），元数据在
`experiments/`。两个目录都不入库。

评估纪律：

1. 绝对收益先比**全宇宙等权**（`reports/research.md` §2），比不过就是没做选择；
2. 因子有效性看**同流动性档内**的对照，不分档会把流动性溢价当成 alpha；
3. 引用数字带三个标签：哪一段、什么调仓频率、跟谁比；
4. 先看 `effective_days / trading_days`，低于约 70% 的段不与其他段并列。

参数走 `config/settings.yaml` 与 `config/universe.yaml`，不要改代码改交易规则。

---

## 3. 数据

### 3.1 重建与校验

```bash
# 全量构建（4700 只 × 5 年，20 分钟以上）
"C:\Program Files\Python314\python.exe" scripts/build_dataset.py --source gm \
    --start 2020-01-01 --end 2024-12-31 --benchmark 000852 --out data/processed --verify-manifest

# 小样本验证链路
"C:\Program Files\Python314\python.exe" scripts/build_dataset.py --source gm \
    --start 2024-01-01 --end 2024-03-31 --limit-symbols "600000,000001,300001" \
    --min-symbols-per-day 1 --out _probe

python scripts/data_manifest.py verify | show | write
```

`baostock` 源免费、无需 token（`pip install baostock`）。SDK 升级导致字段变化时先用 `--probe` 看返回了什么。
构建内置六条硬校验（README §4.2），不过就报错，**不要放宽校验**。

### 3.2 每日增量更新

```bash
"C:\Program Files\Python314\python.exe" scripts/update_dataset.py --data-dir data/processed_2025_2026
```

只补尾部（默认回退 5 个交易日重取，覆盖数据源对最近几天的修订），按 `(trade_date, symbol)`
精确覆盖，停牌股的旧记录不会丢；合并后重做复权因子单调性与连续性校验，失败**不写盘**；写盘是原子的。
实测与全量重建逐行一致（数值最大差 3.6e-12）。收盘前跑会提示"源还没有更新的行情"并退出，属正常。

基准指数更新失败只打 `[warn]`，行情照常更新——看到这条要处理，否则回测超额会缺尾部。

### 3.3 用别的数据源

把规范文件放进一个目录，`--data-dir` 指过去：

| 文件 | 必需列 |
|---|---|
| `bars.csv` / `.parquet` | `trade_date, symbol, open, high, low, close, volume, amount`（建议 `adj_close, pre_close, limit_up, limit_down`） |
| `instruments.csv` | `symbol, name, list_date, delist_date` |
| `status.csv` | `trade_date, symbol, is_st, is_suspended` |
| `calendar.csv` | `trade_date` |
| `benchmark.csv`（可选） | `trade_date, symbol, close` |

接入前必须确认：有没有已退市股票、`adj_close` 是不是后复权、财务数据带不带公告日。

---

## 4. 写自己的策略

继承 `Strategy`，只输出目标权重：

```python
import pandas as pd
from a_share_quant.backtest.costs import CostModel
from a_share_quant.backtest.engine import BacktestConfig, BacktestEngine
from a_share_quant.data.loader import LocalFileProvider
from a_share_quant.factors import Momentum
from a_share_quant.metrics.performance import summarize
from a_share_quant.portfolio.construction import PortfolioRules
from a_share_quant.strategies.base import Strategy, TargetPortfolio
from a_share_quant.universe.builder import UniverseBuilder, UniverseRules


class TopNMomentum(Strategy):
    name = "topn_momentum_20d"
    warmup = 20          # 声明暖机期：窗口不够时引擎会告警

    def __init__(self, top_n: int = 5):
        self.factor = Momentum(window=20)
        self.top_n = top_n

    def generate_target(self, as_of, bars, universe):
        ts = pd.Timestamp(as_of).normalize()
        scores = self.factor.compute_as_of(bars, ts)      # 已截断到 <= as_of
        scores = scores.reindex(list(universe)).dropna().sort_values(ascending=False)
        picked = list(scores.index[: self.top_n])
        w = 1.0 / len(picked) if picked else 0.0
        return TargetPortfolio(as_of=ts, weights={s: w for s in picked})


provider = LocalFileProvider(r"D:\a_share_quant\data\interim\synthetic")
engine = BacktestEngine(
    provider=provider,
    strategy=TopNMomentum(top_n=5),
    universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=60)),
    portfolio_rules=PortfolioRules(max_positions=5, max_weight_per_stock=0.30),
    cost_model=CostModel(),
    config=BacktestConfig(start_date="2021-01-04", end_date="2023-12-29",
                          rebalance_frequency="M"),
    max_participation_rate=0.10,   # 库默认 0（关闭）
)
print(summarize(engine.run()))
```

合成数据上的收益没有任何参考价值。写新因子必须在 `tests/test_no_lookahead.py` 加篡改测试。

---

## 5. 实盘流程

实盘不做选股：冻结样本等权 + 按可承受回撤定仓位（依据见 `reports/research.md` §8）。

### 5.1 资金与仓位

- **仓位**：按能承受的最大回撤倒推，用最坏样本（2015-2019）：-15% → `0.23`、-25% → `0.39`、-35% → `0.55`；
- **资金下限**：100 只、40% 仓位时总资产约需 **80 万以上**。资金不够时 `live_signal.py` 会拒绝写委托，
  并打印买齐所需总资产（README 限制第 29 条）。**调小 `--position` 只会更糟**（每只预算更少）；
  资金不够只能加资金，或用更小的 `--n` 重新冻结样本（`--create-sample --force`，等于换一套组合）。
  命令行 `--n` 与样本文件不一致时以样本文件为准。

### 5.2 第一次：冻结样本（只做一次）

```bash
python scripts/live_signal.py --data-dir data/processed_2025_2026 --create-sample --n 100 --pool 1000
```

生成 `live/sample.json`（完整 1000 只排序，取前 100 只，退市/ST 按名次补位）。仓库里已有一份，
**之后不要再改**——换了它等于换了一套组合。

### 5.3 每个交易日收盘后

```bash
"C:\Program Files\Python314\python.exe" scripts/update_dataset.py --data-dir data/processed_2025_2026
python scripts/live_signal.py --data-dir data/processed_2025_2026 \
    --cash <券商可用现金> --holdings live/holdings.csv --position 0.40
```

非季度月（1/4/7/10 月以外）只检查、不写委托：会列出已不可投资、需要清仓的持仓等告警。
退出码仍为 0（README 限制第 27 条），**要看输出**。

### 5.4 季度调仓日（1/4/7/10 月）

1. 收盘后更新数据、出信号（同 5.3）。首次建仓不传 `--holdings`：

   ```bash
   python scripts/live_signal.py --data-dir data/processed_2025_2026 --cash 1000000 --position 0.40
   ```

2. 看输出与 `live/<日期>/`：

   | 文件 | 内容 |
   |---|---|
   | `target.csv` | 目标持仓：代码、参考价、目标股数、权重、当前股数、是否可交易 |
   | `orders.csv` | 只含需要动的单（单笔 < 2,000 元不下） |
   | `summary.json` | 总资产、可投金额、买卖金额、资金不足名单、告警 |

3. 次日开盘**先卖后买**，按 `orders.csv` 下单；
4. 收盘后从券商导出当日成交，对账并生成新持仓（5.5）。

`live_signal.py` 的硬规则（都有单测）：买卖都按 100 股整数倍，只有清仓可带零股；停牌与已持有的跌停股
冻结（算进仓位、不下单）；未持有的涨停股不进样本；样本外持仓清仓；同一天重复跑零订单；
数据超过 15 天未更新拒绝出信号；资金不足比例超过 `--max-unfunded`（默认 10%）拒绝写委托。
**非 0 退出 = 没写委托文件。**

### 5.5 成交对账

```bash
# 首次建仓（成交前空仓）
python scripts/reconcile.py --orders live/2026-10-20 --fills fills/2026-10-21.csv \
    --save-holdings live/holdings.csv --from-empty
# 之后每次
python scripts/reconcile.py --orders live/<信号日> --fills fills/<成交日>.csv \
    --holdings live/holdings.csv --save-holdings live/holdings.csv
```

成交表两种格式都认（UTF-8 或 GBK）：

```csv
symbol,side,shares,price,fee
600519,buy,100,1331.88,33.30
```

或券商中文导出：`证券代码 / 买卖标志（或买卖方向、操作）/ 成交数量 / 成交价格（或成交均价）/ 费用合计`。
同一字段出现两个候选列（如成交价格和成交均价）会拒绝，删掉一列再跑；费用只认合计列，
手续费、佣金这类分项不会被当成总费用。股数必须是正整数，方向放在 `side` 列。

输出：股数成交率、加权滑点（相对信号日收盘价，正数 = 吃亏）、持仓偏离（**最重要**），
明细写在 `live/<信号日>/`（`orders_vs_fills.csv`、`holdings_gap.csv`、`reconcile.json`）。

| 退出码 | 含义 | 写了什么 |
|---|---|---|
| 0 | 正常 | 报告 + 新持仓 + 成交指纹 |
| 1 | **这批成交已经入过账**（股数指纹在持仓的历史里） | 什么都没写 |
| 2 | 必须人工处理：超卖、成交多于委托、成交找不到委托；或输入错误（文件不存在、缺列、列名歧义、`--holdings` 路径不存在、`--save-holdings` 已存在却没传 `--holdings`） | 业务问题只写报告；输入错误什么都没写 |

**链式脚本遇到非 0 必须停下**。注意：

- `--holdings` 必须是**成交前**的持仓；传成交后的会把同一批成交算两遍；
- 同日分两次回报、股数恰好相同时，加 `--force` 入账。**不要删 `.digest` 文件**来解除保护；
- 完全未成交、部分成交只列出、不报错，要能说出原因（跌停/停牌/资金）。

### 5.6 每周必做：券商持仓核对

指纹只防重复入账。改单、分红送股、漏对一笔、回报延迟都防不住。每周一次：

1. 从券商"持仓查询"导出持仓；
2. 与 `live/holdings.csv` 逐只比对股数；
3. 不一致就往前翻 `live/<日期>/reconcile.json` 找第一次出现偏差的日子，修正 `live/holdings.csv`。

`live/sample.json` 入库；每日委托、持仓、成交（`live/*/`、`live/holdings.csv*`、`fills/`）是个人交易记录，不入库。

---

## 6. 排错

| 现象 | 处理 |
|---|---|
| `ModuleNotFoundError: a_share_quant` | 解释器选错，或重装 `pip install -e ".[dev]"` |
| `bars file not found` | `--data-dir` 指错 |
| `DataValidationError` | 修数据，不要放宽校验 |
| `[warn] survivorship` | 认真处理：数据源可能缺退市股或退市标记 |
| `NotImplementedError: 市值过滤需要...` | 启用了 `universe.yaml` 的预留项 |
| `数据最后交易日 …，距今 N 天` | 先跑 `update_dataset.py`；`--as-of` 指定过去日期也会触发 |
| `资金不足的目标 X/Y 只，超过 --max-unfunded` | 加资金或调大 `--position`；接受偏离才调大 `--max-unfunded` |
| `持仓文件不存在` | `--holdings` 路径打错；成交前确实空仓就去掉 `--holdings`、加 `--from-empty` |
| `这批成交已经入过账` | 正常拦截；确认是另一批真实成交才加 `--force` |

### VS Code

`.vscode/`（不入库）里已写死解释器绝对路径；`launch.json` 里不要用 `${env:USERPROFILE}`（不会展开，
debugpy 会丢盘符）。终端里激活虚拟环境对 F5 调试无效，调试器只看配置里的解释器。
