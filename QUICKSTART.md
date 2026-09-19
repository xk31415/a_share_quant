# 5 分钟上手

> 所有命令均在本机实际验证过。
> **回测结果不代表未来收益。本项目用于研究，不构成投资建议。**
> 当前未接入真实 A 股数据，任何回测数字都不具备市场含义。

---

## 0. 你的环境已经就绪，不需要重装

这个项目已经装好并注册到 Python 环境了，**跳过安装步骤直接用**：

| 项 | 值 |
|---|---|
| 项目目录 | `D:\a_share_quant` |
| 解释器 | `C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe` |
| 安装方式 | 已完成 `pip install -e ".[dev]"`（可编辑模式，改源码立即生效） |
| 合成数据 | 已生成在 `data/interim/synthetic/`（30 只 × 780 交易日） |

验证一下：

```bash
"C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -c "import a_share_quant; print(a_share_quant.__file__)"
# -> D:\a_share_quant\src\a_share_quant\__init__.py
```

> 因为是可编辑安装，**在任意目录都能 import**，脚本也可以从别处调用，不必设 `PYTHONPATH`。

---

## 1. 跑测试（改完代码第一件事）

```bash
cd /d/a_share_quant
"C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m pytest
# -> 75 passed
```

只跑某一类：

```bash
... -m pytest tests/test_no_lookahead.py      # 未来函数专项
... -m pytest tests/test_backtest.py          # T+1 / 涨跌停 / 停牌 / 净值
... -m pytest -k universe                     # 按名字挑
```

---

## 2. 跑第一次回测

合成数据已生成，直接跑：

```bash
cd /d/a_share_quant
"C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe" \
    scripts/run_baseline_backtest.py \
    --data-dir data/interim/synthetic \
    --start 2021-01-04 --end 2023-12-29
```

参数可以在命令行临时覆盖，不用改配置文件：

| 参数 | 作用 |
|---|---|
| `--start` / `--end` | 回测区间 |
| `--freq D|W|M` | 调仓频率（默认取 `config/settings.yaml` 的 `M`） |
| `--data-dir` | 数据目录 |
| `--config` / `--universe-config` | 换配置文件 |

例：改成周频调仓

```bash
... run_baseline_backtest.py --start 2021-01-04 --end 2022-12-30 --freq W
```

> 注意 `--data-dir` 里的路径含 `synthetic` 时，实验记录会自动打上
> "synthetic random-walk data — NOT real market data" 的标记。

---

## 3. 看结果

输出在 `results/EXP-<时间戳>/`：

| 文件 | 看什么 |
|---|---|
| `equity_curve.png` | **先看这张**——净值曲线 + 回撤图 |
| `metrics.json` | 全量指标（收益/风险/回撤/换手/成本/年度月度分解/行业暴露） |
| `trades.csv` | 逐笔成交，**含 `blocked:*` 被拦截记录**——约束是否生效看这里 |
| `positions.csv` | 每日持仓快照（权重、可卖份额） |
| `cost_contribution.json` | 成本吃掉多少收益 |
| `industry_exposure.csv` | 逐日行业暴露 |

实验元数据在 `experiments/EXP-<时间戳>.json`（含数据版本指纹 + Git commit，用于复现）。

**快速判断一次回测是否可信**，看三处：

1. `n_blocked_trades` 是否 > 0 —— 等于 0 要警惕，说明涨跌停/停牌约束可能没触发；
2. `trades.csv` 里成交日期是否都晚于信号日一天（t→t+1）；
3. 控制台有没有 `[warn]` —— 尤其是 `survivorship` 开头的幸存者偏差告警。

---

## 4. 改参数

**不要改代码改交易规则**，全部走 `config/settings.yaml`：

| 想调什么 | 改哪里 |
|---|---|
| 回测区间、初始资金、基准 | `backtest:` |
| 最大持仓数、单票上限 | `portfolio:` |
| 佣金/印花税/滑点 | `costs:` |
| 涨跌停、停牌、一手股数开关 | `market_rules:` |
| 上市天数、ST/停牌/退市过滤 | `config/universe.yaml` |

`universe.yaml` 里的市值/行业/指数成分过滤是**预留项**——现在启用会直接抛
`NotImplementedError` 而不是静默忽略，因为缺对应历史数据。

---

## 5. 换成你自己的数据

把规范文件放到 **`data/processed/`**（默认读取目录），然后：

```bash
... run_baseline_backtest.py --data-dir data/processed --start 2018-01-01 --end 2023-12-31
```

四个必需文件：

| 文件 | 必需列 |
|---|---|
| `bars.csv` 或 `.parquet` | `trade_date, symbol, open, high, low, close, volume, amount` |
| `instruments.csv` | `symbol, name, list_date, delist_date` |
| `status.csv` | `trade_date, symbol, is_st, is_suspended` |
| `calendar.csv` | `trade_date` |

建议的可选列（有了才能启用更多约束/因子）：`adj_close, pre_close, limit_up, limit_down, industry, market_cap`。

**接入前必须确认的三件事**（这是框架唯一无法自己察觉的隐患）：

1. 数据里**有没有已退市股票**及其 `delist_date` —— 只有当前上市股票会让回测系统性高估；
   `validate_dataset` 会自动告警，别忽略。
2. `adj_close` 是后复权价吗？信号用复权价、成交用原始价，搞反了结果全错。
3. 财务数据带 `announce_date` 吗？没有就无法防止财务数据的未来函数。

---

## 6. 写自己的策略

继承 `Strategy`，只做一件事：输出**目标权重**。读数据、算成本、下单都不归你管。

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
    """20 日动量前 N 名等权持有。"""
    name = "topn_momentum_20d"

    def __init__(self, top_n: int = 5):
        self.factor = Momentum(window=20)
        self.top_n = top_n

    def generate_target(self, as_of, bars, universe):
        ts = pd.Timestamp(as_of).normalize()
        # compute_as_of 已把输入截断到 <= as_of，拿不到未来数据
        scores = self.factor.compute_as_of(bars, ts)
        scores = scores.reindex(list(universe)).dropna().sort_values(ascending=False)
        picked = list(scores.index[: self.top_n])
        if not picked:
            return TargetPortfolio(as_of=ts, weights={})
        w = 1.0 / len(picked)
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
)
print(summarize(engine.run()))
```

上面这段可以直接跑。它会输出 `cumulative_return` 等一串指标。

> **别被这个数字骗了。** 在合成随机游走数据上它能跑到 +84%，纯粹是选股噪声 +
> 生成器给每只票的不同漂移率造成的，跟"动量因子有效"没有任何关系。
> 真实数据上重跑之前，这个数字没有任何参考价值。

**写新因子后，必须配一个"篡改未来数据、历史值不变"的测试**加到 `tests/test_no_lookahead.py`——
框架无法静态阻止因子内部写出 `shift(-1)` 这类自造未来，只能靠测试兜。

---

## 7. 常见操作速查

```bash
# 重新生成合成数据（行业分类列也在里面）
... scripts/generate_synthetic_data.py

# 查看某次实验的完整指标
... -c "import json,glob,os; p=sorted(glob.glob(r'D:\a_share_quant\results\EXP-*'))[-1]; print(p, os.listdir(p))"

# 清理本地实验产出（results/ 和 experiments/ 都已 gitignore，不入库）
```

---

## 8. 在 VS Code 里运行和调试

打开项目：

```bash
code D:\a_share_quant
```

### 第一步：选对解释器（最容易踩的坑）

这个项目的虚拟环境在 WorkBuddy 的托管目录里，**不在项目下的 `.venv`**，
VS Code 不会自动发现它，默认会用一个没有 pandas 的 Python，然后满屏 `ModuleNotFoundError`。

我已经写好 `.vscode/settings.json` 指定了正确路径，打开项目时按提示装推荐插件即可。
如果还是不对，手动选一次：

`Ctrl+Shift+P` → `Python: Select Interpreter` → 选
`C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe`

验证方法：新建终端（`Ctrl+\``），运行

```bash
python -c "import a_share_quant; print(a_share_quant.__version__)"
# -> 0.1.0
```

### 第二步：F5 直接跑

`.vscode/launch.json` 里已经配好，**按 F5 选一个就行**：

| 配置项 | 干什么 |
|---|---|
| 回测 · 等权基准（合成数据） | 最常用，跑完整回测 |
| 回测 · 真实数据（data/processed） | 接入真实数据后用这个 |
| Python · 当前文件 | 调试你正在写的脚本 |
| pytest · 全部测试 | 断点调试 75 个测试 |
| pytest · 当前文件 | 只跑光标所在的测试文件 |
| 生成合成数据 | 重新生成合成数据 |

### 第三步：打断点看什么

引擎一天一天地跑 780 次，单步会累死。**用条件断点**：

| 想看什么 | 在哪下断点 | 条件 |
|---|---|---|
| 某个调仓日发生了什么 | `backtest/engine.py` 信号日那段 | `str(d.date()) == "2021-04-30"` |
| 成交价到底取的是哪天的价 | `backtest/execution.py` 的 `execute()` | 直接断，看 `open_px` 和 `row` |
| 为什么这只票没成交 | 同上 | `od.symbol == "SYN0005"` |
| 股票池为什么排除了它 | `universe/builder.py` 的 `get_universe()` | 看 `candidates` 的变化 |
| 因子值算得对不对 | `factors/technical.py` | 用 `compute_as_of` 单独调 |

调试控制台里可以直接查 DataFrame：

```python
bars.loc[bars["trade_date"] == pd.Timestamp("2021-04-30")].head()
result.trades[result.trades["note"] != "filled"]     # 看所有被拦截的成交
```

### 测试面板

装好 Python 插件后，左侧出现**烧杯图标**（Testing）：

- 会列出 75 个测试，可以单跑、单挑一个调试；
- 改完代码自动重新发现；
- 建议先跑 `tests/test_no_lookahead.py` —— 它专门抓未来函数，动过因子或引擎就该跑它。

### 其他小提示

- 回测脚本画图用的是 `matplotlib` 的 `Agg` 后端，**不会弹窗卡住调试**；图存在 `results/EXP-*/equity_curve.png`。
- 想单步进 pandas 内部：用「Python · 当前文件」配置（`justMyCode: false` 已设好）。
- `.vscode/` 目前被 `.gitignore` 忽略了（里面有机器相关的绝对路径）。
  要把它纳入版本管理的话，改 `.gitignore` 删掉 `.vscode/` 那行即可。

---

## 9. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `ModuleNotFoundError: a_share_quant` | 没装或被切了环境 → 重装 `pip install -e ".[dev]"` |
| `FileNotFoundError: bars file not found` | `--data-dir` 指错目录，或目录里没有 `bars.csv/.parquet` |
| `DataValidationError: bars contain N dates not in calendar` | 行情日期和 `calendar.csv` 对不上，检查日历覆盖 |
| `DataValidationError: delist_date < list_date` | 证券表数据有错，修数据而不是放宽校验 |
| `[warn] survivorship: ...` | **认真处理**。数据源可能缺退市股或退市标记 |
| `NotImplementedError: 市值过滤需要...` | 用了 `universe.yaml` 的预留项但没有对应历史数据 |
| 控制台卡住没输出 | 数据量太大时引擎是全量预载的，缩小回测区间 |
