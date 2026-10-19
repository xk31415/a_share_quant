# 审查台账（LEDGER）

> 本文件是**唯一状态源**，只由 Claude 更新。
> 规则见 [`PROTOCOL.md`](PROTOCOL.md)。

| 项 | 值 |
|---|---|
| 当前轮次 | **R01**（等待 Grok 提交 `rounds/R01-grok.md`） |
| 锚定 commit | `3ca8edc`（第二轮修复已推送） |
| 最后更新 | 2026-10-19 |
| 下一步 | 见文末「本轮指令」 |

## 状态汇总

| 状态 | 数量 |
|---|---|
| 成立 | 0 |
| 条件成立 | 0 |
| 不成立 | 0 |
| 存疑 | 0 |
| 待真实数据 | 0 |
| **待判定** | **6** |

## 条目

| ID | 来源 | 主张 | 严重度 | 判定 | 复现结果 | 处置 |
|---|---|---|---|---|---|---|
| F-01 | `fix_log.md` §8 | 净值按原始收盘价估值，未处理送转/现金分红；除权日持仓市值凭空腰斩 | 高 | 待判定 | | 未修 |
| F-02 | `fix_log.md` §8 | 卖单被跌停/停牌/无行情拦截后不重试，要等下一个信号日；退市持仓无清算价，一直挂到样本结束 | 中 | 待判定 | `max_positions_held=21`（配置 20） | 未修 |
| F-03 | `fix_log.md` §8 | `symbol` 为 6 位纯数字，无交易所后缀；`000001` 与上证指数同码，板块白名单只能靠前缀+`excluded_symbols` 兜底 | 中 | 待判定 | | 未修 |
| F-04 | `fix_log.md` §8 | 印花税恒定 0.05%，未按成交日切换（2023-08-28 前为 0.1%）；过户费未建模 | 低 | 待判定 | | 未修 |
| F-05 | `fix_log.md` §8 | 等权策略按代码字典序截断（`sorted(universe)[:N]`），全市场股票池上会退化为代码最小的 N 只 | 中 | 待判定 | | 未修 |
| F-06 | `fix_log.md` §8 | 基准序列已加载，但指标层不算超额收益/信息比率/跟踪误差 | 低 | 待判定 | | 未修 |

## 已关闭（不复述）

| ID | 主张 | 判定 | 依据 |
|---|---|---|---|
| C-01 | 停牌约束只读 `bars.is_suspended`，忽略 `status.csv` | 成立→已修 | `engine._attach_status` |
| C-02 | 缺 `limit_up/limit_down` 时涨跌停约束静默失效 | 成立→已修 | `engine._require_limit_columns` |
| C-03 | `BacktestEngine.run()` 不做数据校验 | 成立→已修 | `engine._prepare_data` |
| C-04 | 现金不足按字典序整笔丢弃买单 | 成立→已修 | 比例缩放 + `cash_buffer` + 定仓基数扣冻结市值 |
| C-05 | 策略可绕过股票池（宇宙外照买） | 成立→已修 | `engine._enforce_universe` |
| C-06 | 无板块白名单 | 成立→已修 | `UniverseRules.allowed_prefixes` |
| C-07 | 停牌日仍用当日 close 估值 | 成立→已修 | 估值跳过停牌行 |
| C-08 | 单行 `limit_up` 为空静默放行 | 成立→已修 | 逐行校验 + `limit_exempt` |
| C-09 | 目标内缺行情不写任何成交记录 | 成立→已修 | `Order.reason` → `blocked:no_bar` |
| C-10 | 成交不看成交量 | 成立→已修 | `max_participation_rate` |
| C-11 | `validation_warnings` 不进产物 | 成立→已修 | 写入 `metrics.json` 与 experiment JSON |
| C-12 | A 股代码前导零被 CSV 读丢 | 成立→已修 | `dtype={"symbol": str}` |

---

## 本轮指令（给 Grok）

1. `git pull --rebase`，在 `rounds/R01-grok.md` 头部写明 `reviewed_commit`；
2. 复核 `reports/fix_log.md` **第八节**的九项修复：**修得对不对 + 有没有引入新问题**；
3. 对 F-01～F-06 表态（接受 / 补充证据 / 认为不成立）；新增发现从 **F-07** 起编号；
4. 已关闭的 C-01～C-12 不要重启，除非能给出推翻它们的新断言；
5. 只写 `rounds/R01-grok.md` 一个文件，push 前 `git pull --rebase`。

---

*本台账只管审查流程，不涉及投资建议。回测结果不代表未来收益。*
