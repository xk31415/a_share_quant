# 修复记录（对应 `project_audit.md`）

> 修复时间：2026-10-18
> 基线 commit：`cb4145d`
> **回测结果不代表未来收益。本项目用于研究，不构成投资建议。**

---

## 一、总览

| 编号 | 问题 | 状态 | 主要改动 |
|---|---|---|---|
| P0-1 | 停牌约束只读 `bars.is_suspended`，`status.csv` 被忽略 | ✅ 已修 | `engine._attach_status`、`execution.execute` |
| P0-2 | 缺 `limit_up/limit_down` 时涨跌停约束静默失效 | ✅ 已修 | `engine._require_limit_columns` |
| P0-3 | `BacktestEngine.run()` 不做数据校验 | ✅ 已修 | `engine._prepare_data` |
| P0-4 | 现金不足按字典序整笔丢弃买单 | ✅ 已修 | `execution.execute`（比例缩放）、`cash_buffer`、定仓基数扣冻结市值 |
| P1-1 | 净值未处理除权除息 | ⏸ **未修（需要设计决策）** | 见 §五 |
| P1-2 | 换手率是双边口径 | ✅ 已修 | `engine.run` 计 `turnover=单边`，新增 `buy/sell_turnover` |
| P1-3 | 建仓前空仓期计入绩效 | ✅ 已修 | `metrics.summarize` 新增 `*_active` |
| P1-4 | 因子暖机不足静默空仓 | ✅ 已修 | `Strategy.warmup` + 引擎校验 |
| P1-5 | 审计报告把 49 笔拦截当成约束生效证据 | ✅ 已修 | `reports/lookahead_audit.md` 勘误 |
| P2-1 | `_load_benchmark` KeyError | ✅ 已修 | `engine._load_benchmark` |
| P2-2 | `min_list_days` 差一（实为 61 天） | ✅ 已修 | `universe/builder.py:118` |
| P2-3 | 字符串 `"False"` 被解析成 `True` | ✅ 已修 | `schema.coerce_bool` + `loader.get_status` |
| P2-4 | 每次调仓整表重读 | ✅ 已修 | `LocalFileProvider` 缓存 |
| P2-5 | 死配置（`data.root`/`consider_st`/环境变量） | ✅ 已修 | `settings.yaml`、`.env.example` |
| P2-6 | `PRICE_USAGE_POLICY` 被当作机制声称 | ✅ 已澄清 | 改为声明性常量的文档说明 |
| P2-7 | status 缺标的时静默放行 | ✅ 已修 | `builder.warnings` |
| P2-11 | 测试固化了停牌 bug | ✅ 已修 | 改为 status 驱动 + 新增回归测试 |
| P2-12 | 无 `tests/conftest.py` | ✅ 已修 | 新增 |
| — | **合成数据的 ST/停牌窗口从未生效**（审查中新发现） | ✅ 已修 | `generate_synthetic_data.py` |

测试从 **75 → 90 项，全部通过**。

---

## 二、P0 修复详情

### P0-1 停牌：以 `status.csv` 为权威来源

**改动**：`BacktestEngine._attach_status()` 在启动时把 status 表合并进 bars。

合并规则（关键设计）：
- 同一 `(trade_date, symbol)` 存在 status 记录 → **status 优先**；
- status 缺该记录 → 沿用 bars 的同名列（兜底，避免数据源只提供 bars 标记时倒退）；
- 两者都缺 → `False`。

这样 `ExecutionSimulator` 不需要知道数据从哪来，只读合并后的列即可；
`FakeProvider` 与 `LocalFileProvider` 两条路径行为一致。

**验证**：

```
[PASS] P0-1 停牌（只有 status 表、bars 无该列）被拦截  <- blocked:suspended
```

端到端：合成数据里 SYN0004 在 2022-06-01~07-15 停牌，回测在 2022-07-01
产生一笔 `blocked:suspended` 卖单（此前该场景 0 笔）。

**回归测试**：`test_backtest.py::test_suspension_blocks_trading_from_status_table`
（bars 完全不带该列，必须拦截）、`::test_suspension_falls_back_to_bars_when_status_missing_row`。

---

### P0-2 涨跌停：缺列即报错

**改动**：`_require_limit_columns()` 在引擎启动阶段检查。`enforce_limit=True` 且
`limit_up/limit_down` 缺列或全为 NaN → 抛 `DataValidationError`，并在报错信息里
说明两条出路（补数据 / 显式关掉开关）。

刻意不用 `pre_close × 10%` 兜底：ST（±5%）、创业板科创板（±20%）、北交所（±30%）、
新股无涨跌幅规则不同，算出来的值比没有更危险。

**验证**：

```
[PASS] P0-2 缺 limit 列应报错  <- DataValidationError
```

**回归测试**：`test_engine_guards.py::test_missing_limit_columns_raises_instead_of_silently_ignoring`、
`::test_all_nan_limit_columns_raises`、`::test_enforce_limit_false_allows_missing_limit_columns`。

---

### P0-3 校验下沉到引擎

**改动**：`run()` 启动时调用 `_prepare_data()`，其中执行 `validate_dataset`
（bars + instruments + status + calendar）。致命问题抛错；非致命问题写入
`BacktestResult.validation_warnings`，脚本去重后打印。

新增 `validate: bool = True` 参数，只在明确知道自己在做什么时关闭。

**验证**：

```
[PASS] P0-3 编程式调用触发幸存者偏差哨兵
```

**回归测试**：`test_engine_guards.py::test_engine_runs_validation_without_script`、
`::test_validation_can_be_disabled_explicitly`。

---

### P0-4 现金分配：不再按字典序饿死

三处改动，缺一不可：

**(1) 建仓预留现金缓冲**（`cash_buffer`，默认 1%）
`generate_orders` 用 `equity × (1 - cash_buffer)` 定仓，覆盖佣金/印花税/滑点。

**(2) 现金不足时按比例缩放全部买单**（`ExecutionSimulator.execute`）
先剔除不可买订单（停牌/涨停/无行情），再算总需求；现金不足则
`factor = cash / need` 统一缩放，而不是按顺序整笔丢弃。缩放后按缺口从大到小执行，
避免舍入余量被代码靠前的标的吃掉。

**(3) 定仓基数扣除"当天卖不掉"的持仓市值**（`_sizing_equity`）

这条是审查时发现的**根因**：退市股卖不掉，其市值仍计入总权益，但每次调仓又按
总权益分配目标权重 → 同一笔钱被重复分配 → 每次调仓都现金不足。修复后：
`sizing_equity = 总权益 − 无法交易持仓市值`。

**修复前后对比**（同一份合成数据、780 日、月频）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `blocked:insufficient_cash` | **21 笔** | **0 笔** |
| `blocked` 合计 | 49 | 29（28 `no_bar` + 1 `suspended`） |
| 被丢弃的标的分布 | 集中于 SYN0019/0020/0022（代码靠后） | 无 |

**回归测试**：`test_backtest.py::test_cash_shortfall_scales_buys_instead_of_dropping`
（cash_buffer=0 强制紧现金，断言无整笔丢弃且首日建满 10 只）。

---

## 三、P1 修复详情

### P1-2 换手率改为单边口径

`turnover = (买入额 + 卖出额) / 2 / 前一日权益`，同时新增 `buy_turnover`
与 `sell_turnover` 两个序列。

**实测**（780 日样本）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `avg_turnover_per_rebalance` | 0.0986 | **0.0517** |
| `cost_bps_per_unit_turnover` | 7.66 | **15.15** |
| `total_turnover` | 3.25 | 1.71 |

数值变化约一倍，属**预期**：旧口径把买卖两侧都算进去了。
**这意味着 v0.1.0 之后的结果不能与更早实验直接比较换手与单位成本**，已在 README 限制 #11 注明。

### P1-3 空仓期不再污染主口径

`summarize` 新增 `first_invested_date`、`n_cash_only_days`、`cumulative_return_active`、
`annualized_return_active`、`annualized_volatility_active`、`sharpe_ratio_active`。

保留全区间口径（便于与历史实验对比），但汇报时应优先用 `*_active`。

### P1-4 暖机期显式告警

`Strategy.warmup`（默认 0）。引擎在启动时比对"首个信号日可用交易日数"，不足则写入
`validation_warnings`，提示"前若干次调仓将产出空目标（全现金）"。

**回归测试**：`test_engine_guards.py::test_warmup_longer_than_window_warns`、`::test_warmup_within_window_is_silent`。

### P1-5 审计报告勘误

`reports/lookahead_audit.md` 结论表已改为"⚠️ 原结论有误"，并附勘误说明，
明确 49 笔拦截的真实构成（28 `no_bar` + 21 `insufficient_cash`，涨跌停/停牌 0 笔）。

---

## 四、P2 修复详情

| 项 | 说明 |
|---|---|
| `_load_benchmark` | 先判断 `adj_close` 列是否存在再取值；缺列回退到 `close`。`test_engine_guards.py::test_benchmark_without_adj_close_falls_back_to_close` |
| `min_list_days` | `cutoff = hist[len(hist) - min_list_days]`，"恰好满 60 个交易日"现在入池。`test_universe.py::test_universe_min_list_days_boundary_is_inclusive` |
| 布尔解析 | 新增 `schema.coerce_bool`：按字面值映射 `"true"/"false"`，不再用 `astype(bool)` 把 `"False"` 变 `True`。`test_data.py` 两个新测试 |
| 数据层缓存 | `LocalFileProvider` 缓存已解析的表；`read_csv` 调用次数 **100+ → 4**。数据文件变动后调用 `clear_cache()` |
| 死配置 | 删除 `settings.yaml` 的 `data:` 块与 `market_rules.consider_st`；`.env.example` 明确写"当前不读取环境变量" |
| `PRICE_USAGE_POLICY` | docstring 改为"声明性常量，不参与运行时强制"，审计报告同步澄清 |
| status 缺标的 | `UniverseBuilder.warnings` 记录"缺 N 个候选标的记录，按非 ST/非停牌放行" |
| 测试固化 bug | `test_suspension_blocks_trading` 改为 status 驱动，并新增 bars 兜底用例 |
| `conftest.py` | 新增，`import helpers` 不再依赖 pytest 的 rootdir 插入 |

---

## 五、未修复项（有意保留）

### P1-1 除权除息估值 —— 需要设计决策，不宜仓促改

净值目前用原始 `close` 估值持仓。遇到 10 送 10，`close` 腰斩，持仓市值凭空少一半。
合成数据里 `adj_close ≡ close`，**这条链路无法用现有数据验证**。

三个候选方案，各有代价：

1. **持仓按"复权折算股数"记账**：`shares_adj = shares × k_entry / k_t`，估值用 `adj_close`。
   正确，但要改 `PortfolioState` 的买卖与成本摊薄逻辑，且 T+1 可卖股数、整手下单都要跟着改；
2. **估值层用价格比率还原**：`value = shares × close_t × k_t / k_entry`。
   改动小，但多次加仓时 `k_entry` 需按市值加权，仍有边界情况；
3. **要求数据源直接提供"已处理公司行动的收盘价"**：最省事，但把责任推给数据源，
   与项目"机制优先"的定位不符。

建议先接一小段真实数据（含一次送转），确认 `adj_close` 与 `close` 的实际关系，
再在方案 1/2 之间选。**在修好之前，真实数据回测的净值不可信。**

### 其他保留项

- 涨跌停板块/ST 规则区分：由数据源提供 `limit_up/limit_down` 解决，代码不推算；
- 基准指数必须出现在 `instruments` 才能通过校验（接口设计遗留，暂用文档说明）；
- `data_version` 用文件名+大小+mtime，不是内容哈希（大文件下刻意取舍）。

---

## 六、行为变更清单（升级注意事项）

1. **停牌以 status 为准**，bars 的同名列降级为兜底；
2. `consider_limit_up_down: true` + 缺 `limit_up/limit_down` → **启动报错**（原来静默忽略）；
3. 引擎默认执行数据校验，可用 `validate=False` 关闭；
4. 新增配置 `portfolio.cash_buffer`（默认 0.01）；
5. 定仓基数扣除无法交易持仓市值（持仓权重合计可能低于 100%）；
6. `turnover` 改单边口径，数值约为原来的**一半**；
7. `summarize` 新增 `*_active` 系列指标；
8. `min_list_days` 边界修正，同样参数下股票池可能**略微变大**；
9. `settings.yaml` 删除 `data:` 与 `consider_st`，新增 `cash_buffer`；
10. **合成数据已重新生成**：ST/停牌窗口改为分配给样本前上市的老股
    （SYN0003 ST、SYN0004 停牌），`data_version` 会变化，
    与 `EXP-20260919-*` 的旧结果不可直接比较。

---

## 七、验收

```bash
cd /d/a_share_quant

# 1) 全部测试
"C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m pytest -q
# -> 90 passed

# 2) 重跑基线（合成数据已重新生成，ST/停牌窗口现在真正生效）
"...python.exe" scripts/run_baseline_backtest.py
# -> blocked:no_bar 28 / blocked:suspended 1 / insufficient_cash 0

# 3) 拦截构成自查：必须分类看，不能只看总数
"...python.exe" -c "
import pandas as pd, glob
t = pd.read_csv(sorted(glob.glob('results/EXP-*'))[-1] + '/trades.csv')
print(t['note'].value_counts())"
```

**当前基线结果**（`EXP-20261018-224804-5865`，仅用于验证框架链路）：

| 项 | 值 |
|---|---|
| 累计收益 | 24.9051% |
| 年化（全区间） | 7.4590% |
| 年化（剔除空仓 63 日） | 8.3231% |
| 夏普（全区间 / 有效区间） | 1.2068 / 1.2867 |
| 最大回撤 | -8.4149% |
| 换手（单边，均值） | 5.17% |
| 总成本 | 2,734.58 元 |

**这些数字仍然只代表随机游走合成数据，没有任何市场含义。**

---

*回测结果不代表未来收益。本项目用于研究，不构成投资建议。*
