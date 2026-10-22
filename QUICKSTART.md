# 5 分钟上手

> 所有命令均在本机实际验证过。
> **回测结果不代表未来收益。本项目用于研究，不构成投资建议。**
> 仓库里**没有行情数据**（体积 + 授权原因，见 README 第 2.1 节），拉下来先按第 5 节
> 重建；本机已建好三份真实数据集，它们的指纹在 `data/MANIFEST.json`。

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
# -> 287 passed
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

1. `trades.csv` 里 `note` 的**分布**——出现 `blocked:suspended` / `blocked:limit_up` /
   `blocked:limit_down` 才说明对应约束被真正触发过；如果只有 `blocked:no_bar`
   （退市股无行情），说明涨跌停/停牌在本次样本里根本没发生，别据此声称"约束已验证"；
2. `trades.csv` 里成交日期是否都晚于信号日一天（t→t+1）；
3. 控制台有没有 `[warn]` —— 尤其是 `survivorship` / `warmup` / `status` 开头的告警。
   引擎启动时会自带一次 `validate_dataset`，不经过本脚本的编程式调用同样会被校验。

---

## 4. 改参数

**不要改代码改交易规则**，全部走 `config/settings.yaml`：

| 想调什么 | 改哪里 |
|---|---|
| 回测区间、初始资金、基准 | `backtest:` |
| 最大持仓数、单票上限、建仓现金缓冲 | `portfolio:` |
| 佣金/印花税/滑点 | `costs:` |
| 涨跌停、停牌、一手股数开关 | `market_rules:` |
| 上市天数、ST/停牌/退市过滤 | `config/universe.yaml` |

`universe.yaml` 里的市值/行业/指数成分过滤是**预留项**——现在启用会直接抛
`NotImplementedError` 而不是静默忽略，因为缺对应历史数据。

`market_rules.consider_limit_up_down: true` 时，bars 必须提供
`limit_up/limit_down`，否则**引擎启动即报错**（不再静默跳过涨跌停判断）。
停牌判断以 `status.csv` 为准，与 bars 上有没有 `is_suspended` 列无关。

---

## 5. 换成你自己的数据

### 5.1 一条命令拉真实数据

```bash
pip install baostock
python scripts/build_dataset.py --source gm --start 2020-01-01 --end 2024-12-31 \
    --benchmark 000300

# 先小样本验证链路（3 只股票 1 季度）
python scripts/build_dataset.py --source baostock --start 2023-01-01 --end 2023-03-31 \
    --limit-symbols 600000,000001,300001 --min-symbols-per-day 1
```

`--source` 可选三种：

| 源 | 登录 | 逐日 ST / 停牌 | 历史涨跌停价 | 退市股 | 备注 |
|---|---|---|---|---|---|
| `baostock`（默认） | 无需 token | ✅ 官方 `isST` | ❌ 规则表推算 | **1197 只** | 免费；高频调用会断连，已加退避重试 |
| `gm` | `GM_TOKEN` | ST 按 `get_symbols` 月度截面还原（最多滞后约 20 个交易日） | ✅ 官方值 | 按 `get_symbols` 截面还原 | 需独立 Python 环境；调用有 5min/1000 次流控 |
| `qmt` | 本地客户端 | 仅停牌 | ❌ | 待测 | 无历史 ST，只能降级 |

> **两个源的分工**：baostock 逐日给官方 `isST`，做状态表更可靠；gm 给官方逐日涨跌停价，
> 用来验证规则表——实测同一区间 66 行，推算值与 gm 官方值 **100% 完全相等**。
>
> **用 gm 时注意**：`get_instruments()` 只返回**最新**上市标的信息（实测只有 24 只已退市），
> 直接拿它当历史全市场会引入幸存者偏差。脚本已改用 `get_symbols(trade_date=...)`
> 按年取截面再求并集（2020-12-31 截面 4140 只、其中 208 只后来退市）。
> 另外 `get_history_instruments` **不返回官方 `is_st`**（请求了也会被丢弃），
> 而且它和 `get_symbols` 返回的 `sec_name` 都是**当前**简称、不是当日简称（2026-10-23 实测），
> **不能**用来判历史 ST。gm 源的 ST 只取 `get_symbols(trade_date=...)` 截面的 `is_st`（README 限制清单第 40 条）。

涨跌停价由 `builders.derive_limits` 按交易所规则推算（主板 10%、ST 5%、
创业板 2020-08-24 后 20%、注册制新股前 5 日无限制），并用**收盘价不得越过涨跌停**
反查规则表——表写错会立刻报错，不会悄悄放宽约束。

脚本内置六条硬校验，任一不过直接报错：

1. `symbol` 必须是 6 位字符串（前导零不能被吃掉）
2. `instruments` 里必须有已退市证券（否则幸存者偏差）
3. 涨跌停价逐行覆盖（`limit_exempt` 之外不许为空）
4. **复权因子 `k = adj_close/close` 单调不减**——k 下降基本等于拿到的是前复权
5. 每个交易日的证券数不能异常瘦（拉取中断的典型症状）
6. 停牌标记与成交量自洽

SDK 升级导致字段名变化时，先用 `--probe` 看它到底返回了什么。

### 5.1.1 校验你拉到的数据是不是"报告里那一份"

**数据本体不入库**（366 MB、单文件 166 MB 超 GitHub 100 MB 限制、且授权跟账号走），
仓库里只有指纹 `data/MANIFEST.json`（17 KB）。重建之后校验一下：

```bash
# 建完就地校验（不一致时退出码 1）
python scripts/build_dataset.py --source gm --start 2020-01-01 --end 2024-12-31 \
    --benchmark 000852 --out data/processed --verify-manifest

# 或者对已有目录批量校验
python scripts/data_manifest.py verify     # 三份一起，退出码 1 = 有差异
python scripts/data_manifest.py show       # 指纹摘要，不读数据
python scripts/data_manifest.py write      # 自己更新过数据后刷新指纹
```

指纹认**内容**不认字节：行按 `(trade_date, symbol)` 排序、浮点按 float64 原位模式、
NaN 先抹平，所以换 pyarrow/pandas 版本重写一遍仍判一致；真正的差异（少几天、
改了某个价格、某列类型变了）会立刻报出来，并**指名是哪几列**：

```
[fail] processed  (D:\a_share_quant\data\processed)
       - bars.parquet 内容不同（差异列：close, adj_close）
```

注意 `processed_2025_2026` 是每日增量更新的窗口，它的指纹会随数据增长而变化——
校验前先确认更新到哪一天，不然会得到"不一致"这个正确但无用的结论。

### 5.2 手工准备数据（用别的源）

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
| `benchmark.csv`（可选） | `trade_date, symbol, close`（基准指数，独立成文件、不进股票池） |

建议的可选列（有了才能启用更多约束/因子）：`adj_close, pre_close, limit_up, limit_down, industry, market_cap`。

> **`limit_up/limit_down` 现在是强约束**：`consider_limit_up_down: true` 时，
> 这两列缺列、或任意一行留空，引擎都会启动失败——留空等于放弃该笔的涨跌停判断。
> 确实没有涨跌幅的日子（注册制新股上市前 5 日）请标 `limit_exempt: true`。

> **板块白名单**：`config/universe.yaml` 的 `allowed_prefixes` 默认只放行沪深主板
> 与创业板，科创板(688/689)、北交所(43x/83x/87x/92x)、B 股(200/900) 会被剔除。
> 你的数据里如果还有 `000300` 这类指数代码，记得放进 `excluded_symbols`。
> 引擎会强制把策略目标与股票池求交，池外代码买不进去，明细见 `universe_violations.csv`。

**接入前必须确认的三件事**（这是框架唯一无法自己察觉的隐患）：

1. 数据里**有没有已退市股票**及其 `delist_date` —— 只有当前上市股票会让回测系统性高估；
   `validate_dataset` 会自动告警，别忽略。
2. `adj_close` 是后复权价吗？信号用复权价、成交用原始价，搞反了结果全错。
3. 财务数据带 `announce_date` 吗？没有就无法防止财务数据的未来函数。

### 5.3 跑策略与因子研究

```bash
# 等权基准（散列抽样 50 只）——注意这是"等权口径"，不是策略
python scripts/run_baseline_backtest.py --data-dir data/processed \
    --start 2020-01-01 --end 2024-12-31 --benchmark 000852

# 12-1 动量（--reverse 选动量最弱的一批）
python scripts/run_baseline_backtest.py --data-dir data/processed \
    --start 2020-01-01 --end 2024-12-31 --strategy momentum --reverse --benchmark 000852

# 低波：--liquidity 0.33 表示只在成交额最高的 1/3 里选（最可交易的区间）
python scripts/run_baseline_backtest.py --data-dir data/processed \
    --start 2020-01-01 --end 2024-12-31 --strategy low_vol --liquidity 0.33 --benchmark 000852

# 因子研究：IC、十分档多空、流动性三档内的长仓绝对收益
python scripts/factor_research.py --data-dir data/processed

# 行业中性低波：每行业取 1 只最低波、跨行业等权（先跑 scripts/fetch_industry.py）
python scripts/run_baseline_backtest.py --data-dir data/processed \
    --start 2020-01-01 --end 2024-12-31 --strategy low_vol --industry-neutral \
    --benchmark 000852

# 行业归因：组合收益里有多少是行业结构、多少是组内选股
python scripts/industry_attribution.py results/EXP-xxxx

# 容量压力测试：放大本金 + 启用容量约束（选股阶段就只挑买得进的）
python scripts/run_baseline_backtest.py --data-dir data/processed \
    --start 2020-01-01 --end 2024-12-31 --strategy equal_weight \
    --capital 500000000 --capacity-aum 500000000 --benchmark 000852

# 并排对比多个实验（样本内 / 样本外 / 不同策略）
python scripts/compare_runs.py --last 6

# 样本外交叉验证：纯收益口径算全宇宙等权，与引擎结果对照
python scripts/crosscheck_ew.py

# 静态容量估算（乐观上限，真实值以重跑为准）
python scripts/capacity.py --exp results/EXP-xxxx
```

**评估纪律**（这几条是踩坑换来的，见 `reports/strategy_log.md`）：

1. 绝对收益先比**全宇宙等权**（2020-2024 月频 9.84%/年；日频 8.72% 含再平衡
   红利，不可用作及格线），比不过就是没做选择；
2. 因子有效性必须看**同流动性档内的等权对照**——低流动 1/3 等权 24%/年，
   高流动 1/3 只有 0.3%，不分档会把流动性溢价当成 alpha；
3. 只做多的策略要报**长仓绝对收益**，光看多空价差没有用（A 股不能便捷做空）；
4. 基准必须从 `benchmark.csv` 读：`000905`/`000852` 与厦门港务/石化机械撞车，
   按代码在 `bars` 里匹配会读到个股价格。

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
    warmup = 20          # 声明暖机期：窗口不够时引擎会告警，而不是静默空仓

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
    max_participation_rate=0.10,   # 库默认 0（关闭）；基线脚本从 settings.yaml 读 0.10
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

我已经在 `.vscode/settings.json` 和 `.vscode/launch.json` 里写死了这个路径
（`C:/Users/67478/.workbuddy/binaries/python/envs/default/Scripts/python.exe`）。

> **两个坑，都踩过了**
>
> 1. **不要写 `${env:USERPROFILE}`**。VS Code 在 launch.json 里不展开它，
>    debugpy 最终拿到的是 `/.workbuddy/binaries/python/...`（丢了盘符），
>    报 `FileNotFoundError: [WinError 2] 系统找不到指定的文件`。
>    必须写死绝对路径。
> 2. **在终端里 `Activate.ps1` 激活虚拟环境，对 F5 调试没用。**
>    调试器用的是配置里指定的解释器，不看终端的 PATH。
>    激活只影响你在终端手敲的命令（激活后提示符会变成 `(default)`）。

改完配置后如果没生效，重载一次窗口：`Ctrl+Shift+P` → `Developer: Reload Window`。

另外建议手动选一次解释器（会影响 Pylance 补全、新建终端时是否自动激活）：

`Ctrl+Shift+P` → `Python: Select Interpreter` → 选
`C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe`

选好后 VS Code 窗口**右下角状态栏**应该显示 `3.13.x ('.workbuddy...')` 之类的字样。

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
| pytest · 全部测试 | 断点调试 287 个测试 |
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

- 会列出 287 个测试，可以单跑、单挑一个调试；
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

---

## 10. 实盘信号（`live_signal.py`）

回测跑通之后，出实盘单用的是另一套脚本。它**不做选股**，只把
「固定样本 + 目标仓位」翻译成委托单——因为三段样本外检验下来
选股与择时都没过门槛（见 `reports/live_readiness.md`）。

### 10.1 第一次：冻结样本（只做一次）

```bash
python scripts/live_signal.py --data-dir data/processed_2025_2026 \
    --create-sample --n 100 --pool 1000
```

生成 `live/sample.json`，里面是**完整 1000 只的排序**，前 100 只入选。

> 这个文件之后不要再改。股票池每月都在变（新股/退市），若每次重排取前 N，
> 截断位置会移动，等于每次重新抽一次——组合会被整体换掉。实测年化差 1.8pp，
> 全是抽样噪声。

### 10.2 每次调仓日：出现单清单

```bash
python scripts/live_signal.py --data-dir data/processed_2025_2026 \
    --cash 110000 --holdings live/holdings.csv --position 0.40
```

| 参数 | 说明 |
|---|---|
| `--cash` | 券商里的**可用现金**。总资产 = 现金 + 持仓市值，程序自己算 |
| `--holdings` | 当前持仓 CSV（`symbol,shares`），不传表示空仓建仓 |
| `--position` | 目标仓位 w。回撤 ≈ w × 满仓回撤，保守取 0.4 |
| `--dry-run` | 只看不下单（不写文件） |
| `--ignore-quarter` | 默认只在 1/4/7/10 月调仓，加它则随时调 |

产出 `live/<日期>/`：

| 文件 | 内容 |
|---|---|
| `target.csv` | 目标持仓：代码、参考价、目标股数、权重、当前股数、是否可交易 |
| `orders.csv` | 下单清单，**只含需要动的**（单笔 < 2000 元不动，省最低佣金） |
| `summary.json` | 总资产、目标仓位、买卖金额、告警 |

### 10.3 几条会直接亏钱的规则（都有单测）

- 买入必须 100 股整数倍；**卖出也是**，只有清仓能带零股（上证函〔2014〕301 号）
- 停牌/跌停的持仓**冻结**：算进仓位、不下单（否则会拿它的市值再买一遍别的票）
- 不在样本内的持仓会被清仓（否则组合慢慢变成"样本 + 一堆历史遗留"）
- 同一天重复跑必须零订单（幂等）
- 数据超过 15 天没更新，直接拒绝出信号

### 10.4 收盘后到下单

```bash
# 1) 15:30 后补数据：只拉最近几天，1~3 分钟（不是 20 分钟全量重建）
python scripts/update_dataset.py --data-dir data/processed_2025_2026

# 2) 出单
python scripts/live_signal.py --data-dir data/processed_2025_2026 \\
    --cash 110000 --holdings live/holdings.csv --position 0.40
```

3. **先卖后买**（A 股卖出资金当日可买入），按 `orders.csv` 顺序执行；
4. 成交后更新 `live/holdings.csv`，下次调用要读它。

> 增量更新默认回退 5 个交易日重取，用来覆盖数据源对最近几天的修订；
> 按 (trade_date, symbol) 精确覆盖，停牌股的旧记录不会丢。
> 实测与全量重建逐行一致（bars 索引相同、数值差 3.6e-12），
> 唯一不同的是 5 只股票在窗口内撤销了 ST——增量版用的截面更新，更准。

> `live/` 下的每日委托单不入库（个人交易记录）；`sample.json` 入库（组合身份）。

### 10.5 成交对账

下单之后核一遍"实际成交"和"模型要的"差多少：

```bash
python scripts/reconcile.py --orders live/2026-10-20 \\
    --fills fills/2026-10-21.csv --holdings live/holdings.csv \\
    --save-holdings live/holdings.csv
```

成交表最少四列（`side` 接受 `buy/sell`、`买入/卖出`、`证券买入/证券卖出`）：

```csv
symbol,side,shares,price,fee
600519,buy,100,1331.88,33.30
```

输出三样东西：

| 指标 | 含义 |
|---|---|
| 股数成交率 | 委托 vs 实际成交，没成交的要能说出原因 |
| 加权滑点（bp） | 实际成交价 vs 信号日收盘价，正数=吃亏 |
| 持仓偏离（元） | 成交后持仓 vs 目标，**这条最重要** |

`--save-holdings` 直接写出下次 `live_signal.py` 要读的持仓表。
`--holdings` 必须传**成交前**的持仓，传成交后的会把同一批成交算两遍。
