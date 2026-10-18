# 项目全面审查报告

> **修复状态（2026-10-18 更新）**：P0 四项、P1 四项（P1-2/3/4/5）及 15 项 P2 已修复并补回归测试，
> 测试从 75 → **90 passed**。仅 P1-1（除权除息估值）有意保留，需要先做设计决策。
> 逐条改动、行为变更与验收方式见 **[`fix_log.md`](fix_log.md)**。
>
> 审查对象：`a_share_quant` v0.1.0（Git HEAD `cb4145d`，工作区干净）
> 审查方式：全量通读 27 个源码模块 + 9 个测试文件 + 配置/脚本，实跑测试与基线回测，并为每个疑点编写**复现探针**验证。
> **回测结果不代表未来收益。本项目用于研究，不构成投资建议。**

---

## 零、结论摘要

框架的**骨架是可信的**：时间对齐机制（t 日信号 → t+1 开盘成交）、策略视野截断、股票池 point-in-time 这三条最容易被做假的链路，确实是机制保证 + 破坏性测试双锁，不是纸面声明。

但**"约束层"存在三处静默失效**，而且都朝同一个方向——**让回测比真实更乐观**。这正是本项目声称最忌讳的错误类型（"缺数据时要显式报错，不静默通过"），却在最关键的成交模拟环节没有贯彻。

| 等级 | 数量 | 性质 |
|---|---|---|
| P0 阻断 | 4 项 | 静默产出错误结论，接入真实数据前必修 |
| P1 严重 | 5 项 | 系统性偏差 / 指标口径失真 / 文档与事实不符 |
| P2 一般 | 12 项 | 边界、健壮性、死配置、可扩展性 |

**一句话判断**：可以继续在这套框架上做研究，但**不能直接相信当前 `results/` 里任何含"约束"或"换手"字样的数字**，也不能相信审计报告里关于约束生效的结论。

---

## 一、审查方法与实跑结果

| 项目 | 结果 |
|---|---|
| `pytest` | **75 passed**（12+6+14+6+7+18+7+5），与 README 声称一致 |
| 基线回测复现 | 数字与 `EXP-20260919-172711-4320` **逐位一致**（累计 24.2763%、夏普 1.125330、成本 2,610.418558） |
| 运行耗时 | 30 只 × 780 日 ≈ **5 秒** |
| 账本自洽性 | `trades.csv` 成本合计 = `costs.csv` 合计 = 2,610.42 ✅ |
| 数据版本指纹 | `b7decabf2d93`，与实验记录一致 ✅ |
| Git commit 追溯 | 已写入实验元数据 ✅ |

> 说明：审查过程新跑了一次回测，产生 `results/EXP-20261018-223648-4014/` 与 `experiments/EXP-20261018-223648-4014.json`。两处均在 `.gitignore` 内，不影响仓库。复现探针脚本放在系统临时目录，未污染项目。

---

## 二、P0 阻断级问题

### P0-1 停牌约束在成交层读错了数据源 → 静默失效

**位置**：`src/a_share_quant/backtest/execution.py:111`

```python
if self.enforce_suspension and bool(row.get("is_suspended", False)):
```

**事实链**：
- 规范里 `is_suspended` 的**权威来源是 `status.csv`**（`schema.py` 中 `STATUS_REQUIRED_COLUMNS`，是**必填**）；
- 在 `bars` 里 `is_suspended` 只是**可选列**（`BAR_OPTIONAL_COLUMNS`）；
- 成交模拟只看 `bars`，**从不查 `status`**；
- 按 QUICKSTART §5 提供的数据如果只带必填列，`row.get("is_suspended", False)` 永远返回 `False`。

**复现证据**（探针 A）：构造 `status.csv` 标记 2021-01-05 停牌、`bars` 仅含必填列的数据集 → 该日**正常成交**，`note="filled"`，无任何告警。

**现网证据**：合成数据 `status.csv` 里 SYN0027 有整整一个月停牌窗口，780 个交易日 / 36 次调仓下来：
- `blocked:suspended` = **0 笔**
- 全部 49 笔拦截 = `blocked:no_bar` 28 + `blocked:insufficient_cash` 21

**影响**：持有停牌股时，可以按停牌期间的价格"卖出"并换成现金或其他股票。回测比真实更乐观，且不留痕迹。

**与文档冲突**：README §13 缺数据清单明确写"每日 ST/停牌状态表 | 股票池**与成交约束**"。实际成交约束完全不看这张表。

**修法**：引擎预载 `status`，按 `(trade_date, symbol)` 建字典传给 `ExecutionSimulator`；或由 `DataLoader` 在加载时把 `status` 合并进 `bars`（推荐后者，一次改动全链路生效）。同时补一条锁定测试：**只有 `status` 标记、`bars` 无该列时，成交必须被拦截**。

---

### P0-2 涨跌停约束在缺列时静默变成空操作

**位置**：`src/a_share_quant/backtest/execution.py:117-121`（买入）、`137-141`（卖出）

```python
limit_up = row.get("limit_up")
if (self.enforce_limit and pd.notna(limit_up) and open_px >= float(limit_up) - 1e-9):
```

`limit_up/limit_down` 是可选列。缺列 → `None` → `pd.notna(None)` 为 `False` → **不拦截、不告警、不报错**。

**复现证据**（探针 B）：删掉 `limit_up/limit_down` 两列，把某日开盘价人为拉高 **+50%**（远超任何板块的涨停限制），配置 `consider_limit_up_down: true` → 仍然成交 66,600 股，`note="filled"`。

**为什么这条比 P0-1 更值得警惕**：项目的股票池模块对预留项的处理是**教科书式的正确**——缺数据就 `raise NotImplementedError`，宁可崩也不静默。同一条原则到了成交层就丢了。用户在 `settings.yaml` 里看到 `consider_limit_up_down: true`，会认为约束已生效。

**修法**：`enforce_limit=True` 且 `bars` 缺 `limit_up/limit_down` 时，在 `run()` 启动阶段直接 `raise`（或至少 `[warn]` 到 stdout 并入实验记录）。**不要**用 `pre_close × 10%` 自行推算——ST（±5%）、创业板/科创板（±20%）、北交所（±30%）、新股无涨跌幅，规则不同，推算出来的值比没有更危险。

---

### P0-3 幸存者偏差哨兵只在脚本路径生效，引擎本身不校验

**位置**：`src/a_share_quant/backtest/engine.py` 的 `run()` —— 全文件**没有** `validate_dataset` 调用（已 grep 确认）。

校验只存在于 `scripts/run_baseline_backtest.py:82`。

**这意味着**：
- 用 `run_baseline_backtest.py` 跑 → 有校验；
- 用 QUICKSTART §6 教的**编程式用法**（直接 `BacktestEngine(...).run()`）→ **零校验**，包括幸存者偏差哨兵；
- 任何自建研究脚本、notebook 都会绕过整套数据防线。

**与文档冲突**：`reports/lookahead_audit.md` §3 风险 #7 原文写"**任何回测运行都会自动执行**"。不成立。

**修法**：把校验下沉到 `BacktestEngine.run()`（或 `LocalFileProvider.__init__`）的启动阶段，脚本只负责打印 warning。这是"机制优先于自觉"这个项目立身之本该有的位置。

---

### P0-4 现金不足时按股票代码字典序"饿死"靠后的标的

**位置**：`backtest/execution.py:126-133`（现金不足整笔丢弃）、`strategies/equal_weight.py` + `portfolio/construction.py:28-29`（按 symbol 排序）、`engine.py:120`（用信号日权益定仓，未预留成本缓冲）

**机制**：目标权重合计 100%，`desired = floor(w × equity / price / lot) × lot`，金额包含滑点后的成交价但**不预留佣金/印花税**。买入订单按 `symbol` 升序依次消耗现金 → 现金在序列末尾耗尽 → 末尾标的 `blocked:insufficient_cash`，**整笔丢弃而非按比例缩减**。

**复现证据**（现网 780 天运行）：
- 211 笔买单中 **21 笔被整笔丢弃**（10%）；
- 被丢弃的标的分布高度集中在代码靠后的一端：

| 标的 | 被拦次数 |
|---|---|
| SYN0022 | 8 |
| SYN0019 | 5 |
| SYN0020 | 2 |
| SYN0007/0008/0010/0015/0016/0021 | 各 1 |

- 期末组合权重合计 **99.53%**，但这不是"接近满仓"的健康结果——它是建仓当天用掉 94.96%、后续想补仓却没钱的结果。

**为什么这是 P0**：它产生的是**与股票代码相关的系统性偏差**。等权策略本应对 20 只股票一视同仁，实际却系统性少买代码靠后的标的。一个以"机制防偏差"为卖点的框架，藏着一个按字典序分配的偏差，性质比幅度严重得多。

**修法**（两条都要）：
1. 定仓时预留现金缓冲：`equity × (1 - cash_buffer)`，`cash_buffer` 至少覆盖双边佣金+印花税+一个 lot 的滑点；
2. 现金不足时**按比例缩减全部买单**，而不是按顺序整笔丢弃。

---

## 三、P1 严重问题

### P1-1 净值用原始收盘价估值，未处理除权除息 —— 接入真实数据会立刻失真

已列在 README 限制 #2，但严重度被低估了。`engine.py:139-142` 直接用 `close` 估值持仓市值。遇到 10 送 10，`close` 当日腰斩，持仓市值凭空蒸发 50%，而账户实际没有任何损失。

合成数据里 `adj_close ≡ close`（无公司行动），所以**这条链路从未被验证过**，`lookahead_audit.md` §5 第 3 条也承认了。

**建议**：升级为"接入真实数据前必须完成"，而不是"已知限制"。正确做法是持仓按**股数 × 复权价折算**，或在估值层显式应用公司行动因子；只在成交层用原始价。

---

### P1-2 换手率是双边口径，导致换手和单位成本双双失真约 2 倍

**位置**：`engine.py:130-133`

```python
day_turnover += f.shares * f.price      # 买单和卖单都累加
turnover_records[d] = day_turnover / max(prev_equity, 1e-12)
```

实测（`EXP-20260919-172711-4320`，2021-05-03）：

| 项 | 值 |
|---|---|
| 买入额 | 21,782 |
| 卖出额 | 35,062 |
| 前一日权益 | ≈ 988,000 |
| `turnover.csv` 记录 | **0.0575** = (21,782 + 35,062) / 988,000 |

业界的"换手率"默认是**单边**（卖出额/权益，或买入额/权益），此处是双边。后果：
- `avg_turnover_per_rebalance = 9.86%`，实际单边约 **4.9%**；
- `cost_bps_per_unit_turnover = 7.66 bps`，按单边口径实际约 **15 bps**；
- 跨策略/跨文献比较时，换手会被高估一倍、单位成本被低估一倍。

**修法**：明确命名为 `two_way_turnover`，或改为单边口径并同时输出 `buy_turnover`/`sell_turnover`。这是口径问题不是 bug，但**必须在字段名或文档里说清楚**，否则任何读这个数的人都会误判。

---

### P1-3 建仓前的空仓期被计入绩效指标

`min_list_days=60` 使得前 60 多个交易日的股票池为空 → 组合全现金 → 净值曲线是一条直线，但这段直线被完整计入年化收益、波动率、夏普。

实测（`EXP-20260919-172711-4320`）：

| 口径 | 累计 | 年化 | 波动 | 夏普 |
|---|---|---|---|---|
| 全部区间（780 日，含 63 日空仓） | 24.2763% | **7.2837%** | 6.4321% | **1.125** |
| 剔除空仓期（首次净值变动起） | 24.8968% | **8.1388%** | 6.7007% | **1.201** |

年化被低估约 11%（相对）、夏普被低估约 6%。空仓期越长（例如 `min_list_days=250`、或日历起点紧贴回测起点），失真越大。

**修法**：`summarize` 增加"有效区间"字段（首个持仓日 → 末日），或直接输出 `n_cash_only_days` 让使用者知情。

---

### P1-4 因子暖机不足时静默产出全现金组合

`Strategy` 接口没有任何 `warmup` 声明，引擎也就无法校验。若策略用 `Momentum(window=250)`，而回测窗口只加载了 `[start, end]` 的行情（`engine.py:85`），前 250 个交易日的因子全为 NaN → 目标权重为空 → 全现金 → **无任何告警**。

当前没暴露这个问题纯属巧合：`min_list_days=60` 让股票池在 60 天后才非空，正好给 20 日动量留了暖机期。一旦换成长周期因子或设 `min_list_days=0`，就会静默空仓一年。

**修法**：`Strategy` 增加 `warmup` 属性；引擎在启动时校验 `warmup + min_list_days <= 窗口长度`，否则显式报错；或在 `get_bars` 时向前多取 `warmup` 个交易日。

---

### P1-5 审计报告把"49 笔拦截"当成"涨跌停/停牌约束生效"的证据

`reports/lookahead_audit.md` §一 结论表：

> | T+1 / 涨跌停 / 停牌 / 整手 | ✅ 约束生效（本轮 49 笔被拦截） |

实际拆解这 49 笔：

| note | 笔数 | 含义 |
|---|---|---|
| `blocked:no_bar` | 28 | 退市股无行情行 |
| `blocked:insufficient_cash` | 21 | 现金不足（见 P0-4） |
| `blocked:limit_up` / `limit_down` | **0** | — |
| `blocked:suspended` | **0** | — |

**一笔涨跌停拦截、一笔停牌拦截都没有。** 原因是合成数据生成器里 `open = prev_close × exp(N(0, 0.006))`，达到 ±10% 需要约 160 个标准差，永远不可能触发；而停牌窗口只写进了 `status.csv`，成交层根本不读（P0-1）。

对一个把"不把回测结果包装成结论"写进纪律清单的项目来说，这条比普通 bug 更伤——**审计报告本身是它最该守住的可信资产**。建议把这张结论表的该行改为 ❌，并在修复 P0-1/P0-2 后重跑。

---

## 四、P2 一般问题

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 1 | `_load_benchmark` 抛 KeyError | `engine.py:190` | `bars.loc[..., ["trade_date","adj_close"]]` 在缺 `adj_close` 列时直接 `KeyError: "['adj_close'] not in index"`，走不到下面的 `close` 回退分支（探针 C 已复现）。应先判列存在 |
| 2 | `min_list_days` 差一 | `builder.py:114` | `cutoff = hist[len(hist) - min_list_days - 1]`，实际要求 **61** 个交易日。实测：恰好满 60 个交易日的标的被排除 |
| 3 | 布尔列字符串解析陷阱 | `schema.py:94`、`loader.py:116-117` | `.fillna(False).astype(bool)`：若 CSV 里是字符串 `"False"`，`bool("False") == True`。实测 3 行 `"False"` 全部变成 `True` |
| 4 | 数据层重复整表读取 | `loader.py:92-124` | 单次回测：`get_instruments` **36 次**、`get_status` **34 次**、`get_calendar` **37 次**，每次都重新 `read_csv` 全表。真实数据（5000 股 × 1200 日）下 I/O 放大 30 倍以上，是接入真实数据后的头号性能瓶颈 |
| 5 | 配置面三处死键 | `settings.yaml:24-32`、`.env.example` | `data.provider`、`data.root`、`market_rules.consider_st`、`A_SHARE_QUANT_DATA_ROOT` **均无任何代码读取**（已 grep 确认）。改这些配置不会有任何效果，但会让人以为改了 |
| 6 | `PRICE_USAGE_POLICY` 是装饰品 | `schema.py:63-75` | 定义并导出了，但**没有任何强制逻辑**引用它。审计报告 §2 把它列为"复权价不混入成交"的机制——实际保证来自因子用 `adj_close`、成交用 `open` 的约定，不是这个对象 |
| 7 | status 缺失标的时默认放行 | `builder.py:130-131` | `st_map.get(s, False)`：状态表里没有的标的，默认"非 ST、非停牌"。缺数据时朝乐观方向 fail-open，与本项目原则相反 |
| 8 | 涨跌停规则未区分板块/ST | `generate_synthetic_data.py:75-76` | 合成数据统一 ±10%。真实数据须由数据源提供 `limit_up/limit_down`（创业板/科创板 ±20%、ST ±5%、北交所 ±30%、新股无限制），不要自己算 |
| 9 | 基准指数必须进 `instruments` | `validation.py:163-168` | 否则 `validate_dataset` 报 "bars contain symbols missing from instruments"。但指数不是证券，这是接口设计缺陷 |
| 10 | `data_version` 不是内容哈希 | `loader.py:126-135` | 用文件名 + size + mtime。复制数据目录、`touch` 文件都会导致版本号变化，削弱"同版本可复现"的语义 |
| 11 | 测试反而固化了 bug 行为 | `tests/test_backtest.py:130-139` | `test_suspension_blocks_trading` 用 `bars.is_suspended` 来测停牌，等于把 P0-1 的错误数据源当成契约锁定。缺"status 驱动停牌"的测试 |
| 12 | 无 `tests/conftest.py` | `tests/` | 靠 pytest 的 rootdir sys.path 插入才能 `from helpers import ...`。换 pytest 版本或 IDE 可能挂；`helpers.py` 应移到 `conftest.py` 或用 `tests/__init__.py` |

---

## 五、已验证为可靠的部分

不是客套——这几条我真的去撞了：

1. **t 日信号 → t+1 开盘成交**：机制在引擎循环里（`pending_weights` 只在下一个交易日开盘被消费），且有测试断言"周五出信号、必须按周一开盘价成交"。没有用到信号日收盘价成交的漏洞。
2. **策略视野截断**：`hist = bars.loc[bars["trade_date"] <= d]`，且有记录型策略断言"策略看到的最大日期 ≤ as_of"。这是真测试，不是形式主义。
3. **股票池 PIT**：`list_date`/`delist_date`/`status` 三条过滤都按 `as_of` 求值；"未来才退市的股票在历史时点仍可投资"有专门测试。
4. **幸存者偏差哨兵的思路**：把"数据源有没有退市股"（数据层）和"股票池逻辑对不对"（逻辑层）分开，意识到前者无法由后者自证——这是很专业的判断，方向完全正确（只是没接进引擎，见 P0-3）。
5. **`industry_exposure` 的 `merge_asof` 向后取值**：行业变更不会污染历史归因，测试锁定了"变更前用旧分类"。
6. **账本自洽**：逐笔成本合计 = 成本序列合计 = 2,610.42，现金/持仓/权益三处对得上，没有凭空造钱。
7. **配置未硬编码**：费率、持仓上限、涨跌停开关、整手都从 `settings.yaml` 读，策略里没有写死交易规则。
8. **文档质量**：README 的"已知限制（诚实清单）"和 QUICKSTART 的排错表是同类项目里少见的水平。问题不在写得差，而在**有三处与代码实际行为不符**。

---

## 六、建议修复顺序

| 顺序 | 动作 | 理由 |
|---|---|---|
| 1 | 把 `status` 合并进 `bars`（或传给执行器），修 P0-1；补锁定测试 | 一处改动消除最大的乐观偏差 |
| 2 | `enforce_limit=True` 缺极限价列 → 启动即报错，修 P0-2 | 把静默变成显式，符合项目原则 |
| 3 | 把 `validate_dataset` 下沉到 `engine.run()`，修 P0-3 | 让机制覆盖所有调用路径 |
| 4 | 现金缓冲 + 按比例缩减买单，修 P0-4 | 消除字典序偏差 |
| 5 | 换手率口径改名或改算法，修 P1-2 | 一行改动，避免所有报告误读 |
| 6 | 绩效指标剔除空仓期，修 P1-3；`Strategy` 加 `warmup`，修 P1-4 | 指标真实性 |
| 7 | 估值层处理除权除息，修 P1-1 | **接入真实数据的前置条件** |
| 8 | 修正 `lookahead_audit.md` 结论表，修 P1-5 | 可信资产不能带错 |
| 9 | 缓存数据层、清理死配置、修 P2 各项 | 接入真实数据前顺手做 |

**不建议现在做**：参数优化、加更多因子、上机器学习。约束层没修干净之前，任何策略结论都建立在不准确的成交模拟上——先修地基。

---

## 七、复现方式

```bash
cd /d/a_share_quant

# 1) 跑全部测试
"C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m pytest -q

# 2) 复现基线回测（应与 EXP-20260919-172711-4320 完全一致）
"...python.exe" scripts/run_baseline_backtest.py \
    --data-dir data/interim/synthetic --start 2021-01-04 --end 2023-12-29

# 3) 检查 49 笔拦截的真实构成（P0-1 / P1-5 的关键证据）
"...python.exe" -c "
import pandas as pd
t = pd.read_csv('results/EXP-20260919-172711-4320/trades.csv')
print(t['note'].value_counts())"
#   filled 374 / blocked:no_bar 28 / blocked:insufficient_cash 21
#   注意：limit_up / limit_down / suspended 均为 0
```

---

*回测结果不代表未来收益。本项目用于研究，不构成投资建议。*
