# 未来函数 / 数据泄露 / 时间对齐审计

> 审计最初在合成数据上完成（2026-09-19），2026-10-22 在三份真实数据集
> （2020-2024 / 2015-2019 / 2025-2026，共 1,073 万行、505 只退市股）上逐项重跑，
> 2026-10-23 撤回了一处属于未来函数的修补（`sec_name` 判 ST）。本文件是当前状态。

---

## 1. 结论

| 类别 | 结论 |
|---|---|
| t 日信号 → t+1 开盘成交 | ✅ 机制保证 + 破坏性测试 |
| 因子/策略读不到未来日期 | ✅ 机制保证 + 破坏性测试 |
| 财务数据按公告日可见 | ✅ 机制就绪（尚无真实财务数据） |
| 股票池 point-in-time（上市天数、退市、ST、停牌） | ✅ 机制保证 + 破坏性测试 |
| 数据源层面的幸存者偏差 | ✅ 哨兵告警；三份数据集均含退市股 |
| 复权口径 | ✅ 三份数据集复权因子 0 次下降，证实是逐日后复权 |
| ST 状态 | ⚠️ 只用当日截面，无未来函数；但月度截面最多滞后约 20 个交易日 |
| 退市整理期 | ⚠️ gm 没有时点信号，`is_st` 在整理期翻回 False（README 限制第 4 条） |
| 行业分类 | ⚠️ 只有当前静态分类，`--industry-neutral` 选股有微弱前视（README 限制第 5 条） |

---

## 2. 机制与锁定测试

破坏性测试 = 故意篡改/构造未来数据，断言历史信号不变。

| 环节 | 机制 | 锁定测试（`tests/test_no_lookahead.py`） |
|---|---|---|
| 因子只读到 t | `Factor.compute_as_of` 先把输入截断到 `trade_date <= as_of` | `test_factor_values_unchanged_when_future_mutated`（t 之后数据 ×10，t 时刻因子值不变；`Momentum/Volatility/AvgAmount` 参数化） |
| 因子 as_of 与全量一致 | 同上 | `test_compute_as_of_equals_full_compute_on_that_date` |
| 策略视野限于 as_of | 引擎只传 `bars[trade_date <= d]` | `test_engine_never_passes_future_bars_to_strategy` |
| 成交价不早于信号 | 信号日收盘生成目标，下一交易日开盘价撮合 | `test_execution_uses_next_day_open_not_signal_day_close` |
| 股票池不受未来状态影响 | `latest_known(status, as_of)` 向后取最后已知状态 | `test_universe_unaffected_by_future_status_changes` |
| 股票池不受未来退市影响 | `delist_date > as_of` 视为当时仍可投资 | `test_universe_unaffected_by_future_delist_dates` |
| 财务按公告日 | `latest_known(..., date_col="announce_date")` | `test_latest_known_excludes_unannounced_fundamentals` |
| 行业归因不用未来分类 | `industry_exposure` 支持逐日快照（`merge_asof` 向后取值），退回静态分类时显式告警 | `test_metrics.py::test_industry_exposure_historical_snapshot_no_lookahead` |
| 数据层幸存者偏差 | `validate_dataset` 内置 `check_survivorship_bias`：全表无退市股、或行情中途消失却未标退市时告警 | `test_data.py::test_survivorship_sentinel_*` |
| ST 只取当日截面 | gm 源 `_attach_st_snapshots` 用 `get_symbols(trade_date)` 截面 + 向后填充，不用 `sec_name` | `test_dataset_build.py::test_gm_is_st_comes_from_dated_snapshot_not_sec_name` |
| 观察池信号不读 T0 之后 | `generate_target` 把行情截到 as_of；题材成员要 `start_date <= as_of` 且未结束；晨报只取当日行；断路权益丢掉 as_of 之后的点。T+1/T+3/T+5 只在回测结束后计算 | `tests/test_hype_watch.py`（`test_future_prices_and_future_theme_do_not_change_todays_weights`、`test_theme_map_respects_start_and_end_dates`、`test_circuit_breaker_trips_at_minus_8_percent_and_ignores_future_equity`） |

**不是未来函数、不要误改**：同一截面内跨个股标准化；用 t 日收盘价算信号（t+1 成交）；
基准与回撤曲线只在报告层使用。

---

## 3. 残余风险

| # | 风险 | 级别 | 说明 |
|---|---|---|---|
| 1 | 因子内部自造未来 | 中 | `compute_as_of` 只保证输入无未来行，挡不住 `shift(-1)`、`rolling(center=True)`、跨时间标准化。新因子必须配篡改测试（README §9） |
| 2 | 定仓用 t 日权益、成交用 t+1 开盘价 | 低 | 隔夜跳空导致权重偏离，属保守做法，不是未来函数 |
| 3 | 区间最后一个信号日不成交 | 低 | 没有 t+1 可执行，属正确行为；`turnover/costs` 条数可能比信号日少一条 |
| 4 | `turnover/costs` 索引是成交日 | 低 | 与信号日序列对齐时会错位一格 |
| 5 | 停牌持仓按最后已知价估值；退市日按最后已知价强平 | 中 | 平滑停牌期风险；退市整理期真实价格通常更低，终值偏乐观 |
| 6 | 已发布退市风险警示的股票在退市日前仍可投资 | 低 | 需要当时已公告的警示数据，未引入 |
| 7 | 观察窗口与静态题材 | 低 | `build_observations` 用 T+1/T+3/T+5 收盘，只在回测结束后写文件，不进目标权重。`bars.industry` 当题材会告警；静态分类有前视（README 限制第 5、30 条）。缺 `start_date` 的题材表直接报错 |

---

## 4. 真实数据审计结果（2026-10-22）

| 检查 | 2020-2024 | 2015-2019 | 2025-2026 |
|---|---|---|---|
| 交易日历 | 1,212 日，周末交易 0 天，`bars` 越出日历 0 行 | 1,219 日，同左 | 433 日，同左 |
| 退市股与哨兵 | 221 只退市，告警 0 | 249 只退市，告警 1（3 只跨期末长期停牌，见下） | 35 只退市，告警 0 |
| 复权因子 `k = adj_close/close` | 下降 0 次；15,830 个除权日，复权收益与 `close/pre_close-1` 偏差最大 0.000047 | 下降 0 次（构建时剔除 2 只）；最大偏差 0.000337 | 下降 0 次；最大偏差 0.000003 |
| 涨跌停规则 | 主板 10%、ST 5%、创业板改革前 10% / 后 20%；越界 2 行 | 主板 10%、ST 5%、创业板 10%；越界 0 行 | 2026-07-06 起主板 ST 改为 10%；退市整理期首日越界 13 行 |
| 财务数据 | 未接入 | 未接入 | 未接入 |
| 行业覆盖（静态） | 95.3% | 93.3% | 99.2% |

发现与处置：

1. **2015-2019 哨兵告警的 3 只**（`000029`、`000995`、`600610`）是跨期末长期停牌、
   全程 `is_suspended=True`，股票池已排除，不构成幸存者偏差。
2. **复权因子被源重置的 2 只**（`000022`、`000043`）只从 `bars` 剔除、仍留在
   `instruments/status`。构建器已改为三表同步剔除（`purge_dropped_symbols`），
   但现有数据集未重建，两只仍是孤儿代码；17 组实验均未抽中（README 限制第 6 条）。
3. **退市整理期**：原 `*ST` 股进入整理期后 gm 截面 `is_st` 翻回 False（7 / 15 / 9 只），
   整理期首日无涨跌幅但源表仍给 ±10% 跌停价。后者由 `assemble` 默认告警、显式
   `--exempt-limit-breach` 才豁免；前者没有时点信号，未修（README 限制第 4 条）。
4. **gm `sec_name` 是当前简称**（2026-10-23 实测）：385 只 2020-2024 间 ST 状态变过的股票，
   每只 5 年里只有一个简称；"简称带 ST"与官方 5% 涨跌幅几乎无关。曾用它补判 ST、
   已撤回（README 限制第 3 条）。现有数据集构建于该改动之前，未受影响：2020-2024
   `is_st` 与官方涨跌幅一致率 98.83%，余差是月度截面滞后。
5. **2026-07-06 主板 ST 涨跌幅新规**（±5% → ±10%）已写进规则表
   （`builders.py::limit_ratio`，`MAIN_ST_REFORM`）。

接入真实财务数据时补验：用带 `announce_date` 的数据跑一遍 `latest_known`。

---

*回测结果不代表未来收益。本项目用于研究，不构成投资建议。*
