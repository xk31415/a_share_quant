# A 股量化策略研究与回测框架

一个**研究基础设施优先**的 A 股量化研究项目。目标不是立即寻找"最赚钱的策略"，
而是建立严谨、可重复、可扩展、尽可能避免未来函数与数据泄露的研究框架。

> **回测结果不代表未来收益。**
> **本项目用于研究，不构成投资建议。**
>
> 当前项目尚未接入真实 A 股历史数据；在接入之前，任何回测数字
> （包括合成数据冒烟测试的数字）都不具备市场含义。

---

## 1. 项目用途

> **想直接上手操作，请看 [`QUICKSTART.md`](QUICKSTART.md)**（含本机已验证的命令与常见问题排查）。

- 建立可长期沉淀的 A 股因子/策略研究流水线；
- 以机制（而非自觉）防止未来函数、幸存者偏差与数据泄露；
- 用统一的实验记录保证每一次回测可复现。

## 2. 项目结构

```
a_share_quant/
├── README.md / pyproject.toml / .gitignore / .env.example
├── config/
│   ├── settings.yaml        # 回测区间、基准、资金、成本、调仓频率等全局设置
│   └── universe.yaml        # 股票池过滤规则（按 as_of 时间点求值）
├── data/
│   ├── raw/                 # 原始外部数据（不入库）
│   ├── interim/             # 中间数据（含 synthetic/ 合成冒烟数据）
│   ├── processed/           # 规范化数据（LocalFileProvider 读取）
│   └── metadata/            # 日历等元数据
├── src/a_share_quant/
│   ├── data/                # schema、loader（DataProvider 接口）、validation、asof
│   ├── universe/            # 动态股票池（point-in-time）
│   ├── factors/             # 因子框架 + 示例因子（动量/波动率/成交额）
│   ├── strategies/          # 策略接口 + 等权基准策略
│   ├── portfolio/           # 目标权重构建（数量/单票上限）
│   ├── backtest/            # 引擎、执行模拟、成本、持仓（T+1）
│   ├── metrics/             # 绩效指标（收益/风险/回撤/换手/分解报表）
│   ├── research/            # 实验记录（元数据 + Git commit）
│   └── utils/               # 交易日历
├── tests/                   # pytest 测试（含未来函数专项测试）
├── scripts/                 # 合成数据生成、基准回测入口
├── notebooks/ experiments/ results/ reports/
│   └── review/              # 交叉审查协议与台账（见 reports/review/PROTOCOL.md）
```

## 3. 安装

要求 Python >= 3.10。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

核心依赖刻意保持精简：`pandas / numpy / scipy / matplotlib / pyyaml`（dev 另有
`pytest / pyarrow`）。不预装一堆用不上的量化库。

## 4. 如何配置

- `config/settings.yaml`：回测区间、基准（默认沪深300）、初始资金、
  佣金（双边 0.025%、最低 5 元）、印花税（卖出 0.05%）、滑点（0.05%）、
  调仓频率（默认月度）、最大持仓数（20）、单票上限（10%）、
  建仓现金缓冲（1%，覆盖费用与滑点）、涨跌停/停牌开关、整手 100 股。
- `config/universe.yaml`：股票池规则——上市满 60 个交易日、剔除 ST/停牌/退市、
  **板块白名单 `allowed_prefixes`**（默认只留沪深主板 600/601/603/605/000/001/002/003
  与创业板 300/301，排除科创板 688/689、北交所 43x/83x/87x/92x、B 股 200/900）、
  `excluded_symbols`（如基准指数代码）。
  指数成分、市值、行业过滤为预留项（启用即报错）；流动性下限已实现。
  **ST 过滤只在这里配置**（`settings.yaml` 不再有重复开关）。
- 数据目录由命令行 `--data-dir` 指定（默认 `data/interim/synthetic`）；
  当前代码**不读取任何环境变量**，`.env.example` 仅为将来接入数据源 API 预留。

**交易规则与数据源一律在配置中，禁止硬编码进策略。**

## 5. 如何运行测试

```bash
python -m pytest tests/
```

133 个测试覆盖：
- 日期对齐、交易日历（`test_dates.py`）；
- 数据 schema 与校验，含**幸存者偏差哨兵**（`test_data.py`）；
- 因子计算正确性（`test_factors.py`）；
- 股票池 point-in-time（`test_universe.py`）；
- T+1、涨跌停、停牌、佣金/印花税/滑点、组合权重、净值、单边换手（`test_backtest.py`、`test_costs.py`）；
- 绩效指标，含行业暴露与成本归因（`test_metrics.py`）；
- **引擎启动守卫**（`test_engine_guards.py`）：缺涨跌停价必须报错、编程式调用也要跑数据校验、
  暖机期不足必须告警、基准缺 `adj_close` 不得崩；
- **交易权限与数据契约**（`test_constraints.py`）：板块白名单、引擎强制股票池、
  停牌日估值冻结、逐行涨跌停缺失必须报错、`limit_exempt` 豁免、成交量上限、
  目标内缺行情必须留痕；
- **数据装配**（`test_dataset_build.py`）：板块判定、指数剔除、状态/涨跌停合并、
  涨跌停规则表（含创业板改革与 ST 收窄）、六条自检各自能拦住对应坏数据；
- **公司行动**（`test_corporate_actions.py`）：10 送 10 与现金分红的除权日收益≈0、
  除权后涨跌按复权口径计量、缺 `adj_close` 必须告警、无公司行动时等价于原始价；
- **未来函数专项**（`tests/test_no_lookahead.py`）：
  - 篡改未来数据不得改变历史信号；
  - 成交价必须是 t+1 开盘价而非信号日收盘价；
  - 策略永远看不到 `as_of` 之后的数据；
  - 财务数据在公告日前不可见；
  - 行业分类变更不得污染历史暴露归因；
  - 股票池不受未来状态/未来退市影响。

完整的时间对齐与未来函数审计见 **`reports/lookahead_audit.md`**。

## 6. 如何运行回测

```bash
# (a) 生成合成数据（仅用于框架冒烟测试，非真实市场数据）
python scripts/generate_synthetic_data.py

# (b) 运行等权基准回测（验证数据/股票池/调仓/T+1/成本/净值/绩效整条链路）
python scripts/run_baseline_backtest.py \
    --data-dir data/interim/synthetic --start 2021-01-04 --end 2023-12-29

# (c) 接入真实数据后：把规范文件放入 data/processed/ 再运行
#     需要的文件：bars.csv|parquet, instruments.csv, status.csv|parquet, calendar.csv
```

结果输出到 `results/EXP-<id>/`（净值、成交、持仓、换手、成本、指标、图），
元数据写入 `experiments/EXP-<id>.json`。

每次运行产出的文件：

| 文件 | 内容 |
|---|---|
| `equity_curve.csv` / `equity_curve.png` | 净值曲线与回撤图 |
| `positions.csv` | 每日持仓快照（含权重、可卖份额） |
| `trades.csv` | 逐笔成交，**含被拦截记录**（`blocked:*`） |
| `turnover.csv` / `costs.csv` | 单边换手（`(买入额+卖出额)/2/权益`）与成本（索引为成交日 t+1，非信号日） |
| `annual_returns.csv` / `monthly_returns.csv` | 年度/月度收益分解 |
| `metrics.json` | 全量指标 + 成本归因 + 行业暴露 |
| `cost_contribution.json` | 成本归因明细 |
| `industry_exposure.csv` | 逐日行业暴露权重 |

> 注意：`turnover`/`costs` 的索引是**成交日（t+1）**，不是信号日，做时序对齐时勿错位一格。

## 7. 数据模型（重要）

| 用途 | 使用的价格 | 说明 |
|---|---|---|
| 因子/信号计算 | `adj_close`（后复权） | 避免除权除息造成的假跳空 |
| 成交模拟 | `open/high/low/close`（原始） | 真实可成交价格 |
| 涨跌停约束 | `limit_up/limit_down/pre_close`（原始） | 交易所规则基于原始价 |
| 持仓估值 | 原始价 × 复权因子比 `k_t/k_entry` | `k = adj_close/close`，抵消除权跳空 |
| 财务数据 | 必须带 `announce_date` | t 日信号只能用公告日 <= t 的行 |

数据规范（`src/a_share_quant/data/schema.py`）：
- 行情 long format，一行 = (trade_date, symbol)，必填 OHLCV+amount；
- 证券信息含 `list_date/delist_date`（退市股必须保留，防幸存者偏差）；
- 每日状态表逐交易日覆盖 `is_st/is_suspended`。

**停牌/ST 的权威来源是状态表**。引擎在启动时把 `status.csv` 合并进 bars
（同一 `(trade_date, symbol)` 以 status 为准，bars 的同名列只在缺 status 记录时兜底），
因此成交模拟的停牌拦截不再依赖 bars 的可选列。

**涨跌停必须由数据提供** `limit_up/limit_down`。`consider_limit_up_down: true`
但 bars 缺这两列、或**任意一行**为空值时，引擎**启动即报错**——留空会被当成
"没有涨跌停"而静默放行。不用 `pre_close × 10%` 自行推算，
因为 ST（±5%）、创业板/科创板（±20%）、北交所（±30%）、新股无涨跌幅规则各不相同。
确实没有涨跌幅的（注册制新股上市前 5 日）请用 **`limit_exempt: true`** 显式标注。

**停牌日的行情不参与估值**。数据商在停牌日可能仍给一根理论价 bar，直接拿来
估值会让净值凭空跳变；引擎只接受非停牌日的收盘价。

## 8. 防未来函数机制

- **交易权限**：股票池是"能买什么"的定义，不是给策略的参考意见。引擎在生成
  目标权重后与当日股票池**求交**，池外代码一律不下单，并写入
  `universe_violations.csv` 与 `validation_warnings`——策略即使返回了被剔除的
  ST/科创板/北交所代码，也买不进去；
- **数据校验**：`BacktestEngine.run()` 在启动阶段自行执行 `validate_dataset`，
  **不依赖调用方脚本**——编程式用法（QUICKSTART §6）同样会跑数据校验与
  幸存者偏差哨兵；致命问题直接抛错，非致命告警写入 `result.validation_warnings`；
- **时间对齐**：t 日收盘生成信号，t+1 开盘成交（引擎强制，有测试锁定）；
- **策略视野**：引擎只向策略传 `trade_date <= as_of` 的行情（有测试锁定）；
- **股票池**：`get_universe(as_of)` 只用当时已知信息（上市天数、当时未退市、
  当时状态），未来退市/ST 不影响历史时点；
- **财务数据**：`latest_known(..., date_col="announce_date")` 按公告日取数；
- **因子**：因子只做后视计算（rolling/shift），`compute_as_of` 截断输入；
- **幸存者偏差哨兵**：`check_survivorship_bias` 已接入 `validate_dataset`，
  发现「全表无退市股」或「行情中途消失却未标记退市」时告警（不静默通过）。
  *为什么需要它*：股票池逻辑能在历史时点正确剔除未来退市股，前提是**数据源真的提供了退市股**——
  若数据源只有当前上市股票，回测会被系统性高估，而股票池逻辑本身察觉不到；
- **行业暴露归因**：优先取 `bars.industry` 逐日快照（按 as_of 向后取值），
  退回 `instruments` 静态分类时会在 stdout 显式告警；
  用"最新行业分类"回溯历史会在归因上引入前视偏差。

## 9. 如何新增因子

1. 在 `src/a_share_quant/factors/` 新建类，继承 `Factor`；
2. 声明 `name`、`window`（暖机期）、`uses_adjusted`（是否后复权价）；
3. 实现 `compute(bars) -> DataFrame`（trade_date × symbol，缺失为 NaN）；
4. docstring 写明：数学定义、数据输入、信号形成时间、复权方式；
5. 在 `tests/test_factors.py` 加手工可验证的正确性测试；
   在 `tests/test_no_lookahead.py` 加"篡改未来数据不变历史值"测试。

参考实现：`factors/technical.py` 的 `Momentum / Volatility / AvgAmount`。

## 10. 如何新增策略

1. 继承 `Strategy`，实现
   `generate_target(as_of, bars, universe) -> TargetPortfolio`；
2. 只输出目标权重（symbol -> weight）；不做 I/O、不算成本、不下单；
3. 因子计算在策略内部调用因子框架完成；
4. 用 `scripts/run_baseline_backtest.py` 的模式写运行入口（或扩展它）。

参考实现：`strategies/equal_weight.py`。

## 11. 如何保存实验

每次运行 `run_baseline_backtest.py` 自动记录：
experiment_id、策略名、数据版本指纹、股票池规则、区间、频率、
成本假设、参数、基准、结果路径、**Git commit**、时间戳。

查看：`experiments/EXP-*.json`；复现：同一 commit + 同一数据版本 + 同一配置。

## 12. 当前已知限制（诚实清单）

1. **没有真实 A 股历史数据**（最重要的限制，见下节）；
2. **公司行动（送转/分红）已在估值层处理**：成交仍用原始价与原始股数，估值按
   `k_t/k_entry`（`k = adj_close/close`）还原，除权日收益≈0。前提是 `adj_close`
   必须是**后复权**序列；若数据源给的是前复权（每次新除权会重写历史因子），
   比值法会失真——`bars` 缺 `adj_close` 时引擎会显式告警；
3. 停牌/退市后持仓冻结在最后已知价（无退市整理期/清算建模）。目标内缺行情的
   标的现在会留下 `blocked:no_bar` 记录，但**不会**被强制清算——终值仍然偏乐观；
4. 调仓日先卖后买仍可能因卖出受阻（跌停/停牌）导致持仓数超过 `max_positions`
   （合成数据里是 21 > 20）。**受阻的卖单不会在次日自动重试**，要等下一个信号日；
5. 买入按"开盘即涨停则不成交"的保守假设，未建模盘中部分成交；
6. 指数成分/市值/行业过滤未启用（缺历史数据，启用时会显式报错而非静默忽略）；
7. 引擎在启动时全量预载行情（内存型），第一阶段规模足够，超大股票池需改分块；
8. 基准对照需要基准指数行情数据，当前数据缺失时不输出对比；
9. 行业暴露目前只有 `instruments` 的**静态**分类可用（合成数据），
   真实数据应提供逐日/逐期行业快照，否则归因存在前视偏差；
10. 因子实现层的"自造未来"（如 `shift(-1)`、跨时间标准化）无法由框架静态阻止，
    必须靠每个新因子配套的篡改测试把关（见第 9 节流程）；
11. **换手率是单边口径**（`(买入额+卖出额)/2/权益`）。旧版本用双边口径，数值约为
    现值两倍，因此 v0.1.0 之后的结果不能与更早的实验直接比较换手与单位成本；
12. 停牌/退市持仓的**市值不计入定仓基数**（否则同一笔钱会被重复分配，导致每次调仓
    现金不足），但这部分资产仍计入总权益，所以持仓权重合计会低于 100%；
13. 绩效指标默认按**全区间**计算，包含建仓前的空仓期（`min_list_days` 暖机）。
    同一份 summary 里另有 `*_active` 字段给出剔除空仓期后的口径，汇报时优先用它；
14. **公司行动缺口已补上**（送转/分红按复权因子还原创除权跳空，合成数据里
    000002 有一笔 2:1 送转做端到端验证）。剩余风险是数据源口径：若 `adj_close`
    是前复权而不是后复权，比值法会失真；这一点只能靠数据侧保证，框架只能告警；
15. `symbol` 是 6 位纯数字，没有交易所后缀，`000001`（深市股票）与上证指数同码。
    板块白名单靠前缀识别，对这类同码只能靠 `excluded_symbols` 兜底，
    长期应改成 `600001.SH` / `000001.SZ` 形式；
16. 印花税用单一税率 0.05%，未按成交日切换（2023-08-28 前实际为 0.1%）；
    过户费未建模。默认区间跨过税率变更点时，早期卖出成本会少算 5 bps；
17. 等权"基准"按代码字典序截断（`sorted(universe)[:N]`），在真实全市场股票池上
    会退化成代码最小的 N 只，不是中性基准。它只是冒烟策略，不要当基准用；
18. 成交量上限（`max_participation_rate`）用的是**成交日全日成交量**，是事后容量代理，
    不是开盘时点真实可成交量。库的默认值是 0（关闭），基线脚本从 `settings.yaml`
    读 0.10；编程式调用请显式传入。`volume` 缺失时引擎会启动报错而不是放行；
19. 卖出申报也按整手约束：持有整百股时，卖出数量必须是 100 股整数倍
    （上证函〔2014〕301 号、深证会〔2014〕72 号）。零股仅在清仓时一次性卖出。

## 13. 当前缺少什么数据

**当前项目缺少真实 A 股历史数据，需要提供数据源或数据文件。** 具体 gap：

| 数据 | 用途 | 现状 |
|---|---|---|
| 日线行情（含退市股、后复权价、涨跌停价） | 信号与成交模拟 | 缺 |
| 上市/退市日期表 | 股票池防幸存者偏差 | 缺 |
| 每日 ST/停牌状态表 | 股票池与成交约束 | 缺 |
| 真实交易日历 | 时间对齐 | 缺（合成数据用近似工作日历） |
| 财务数据（带公告日） | Value/Quality 因子 | 缺 |
| 指数成分变更历史 | 成分股池 | 缺 |
| 基准指数行情 | 绩效对照 | 缺 |

接入任何外部数据源前，必须先明确：数据源是什么、覆盖范围、是否包含退市股、
是否包含历史股票池信息、复权方式、财务数据公告日、授权限制——然后再决定。

数据准备已经脚本化：`scripts/build_dataset.py --source baostock|gm|qmt`（默认 baostock，
免费、无需 token、**含 1197 只退市股**；gm 的退市覆盖只有 24 只，仅用作交叉校验），
产出四张表并执行
六条硬校验（退市股必须存在、涨跌停逐行覆盖、`symbol` 必须字符串、
**复权因子 k 单调不减**、每日证券数、停牌标记自洽）。校验不过直接报错，
不产出"看起来能跑"的数据集。

## 14. 下一步建议（按优先级）

1. **接入真实数据源**（最高优先级）：`pip install baostock` 后
   `python scripts/build_dataset.py --source baostock --start 2020-01-01 --end 2024-12-31`，
   先小样本验证再全量；涨跌停价由规则表推算并自检；
2. 用真实数据重跑等权基准，检查成本/换手/涨跌停拦截比例是否合理；
3. 增加基于真实财务数据（带公告日）的 Value/Quality 因子；
4. 实现周度/月度调仓下的因子排序选股策略（而非等权）；
5. 加入行业暴露、交易成本贡献等更细的组合归因。

## 15. 第一阶段纪律（禁止事项）

- 不自动寻找"最赚钱策略"、不暴力参数优化、不自动挑最高收益参数；
- 不使用未来数据/未来股票池/未来财务数据；
- 不为提高收益修改交易规则；
- 不删除或隐藏失败的回测，不把回测结果包装成投资结论。
