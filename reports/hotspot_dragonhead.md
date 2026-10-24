# 热点 + 龙头（研究规则）

> 本文只定义选股规则、数据依赖和可证伪断言。
> **没有回测数字，不预测收益，不构成买卖建议。**
> 回测结果不代表未来收益。本项目用于研究。

规则写在 `src/a_share_quant/strategies/hotspot_dragonhead.py`，
参数默认值在 `config/settings.yaml` 的 `hotspot_dragonhead`。
这些数字是先验（对齐单票上限），没有按收益搜索过。调仓是日频按需，不是月频。

---

## 1. 规则

信号日只用 `trade_date <= as_of` 的数据。价格用 `adj_close`，没有该列时退回 `close`。

1. **热点**。股票池内每只股票算近 `window` 个交易日收益（末日价 / `window` 日前的价 − 1）。
   按行业取等权平均。有效样本数达到 `min_industry_members` 的行业里，
   收益最高的 `top_k` 个是热点。收益并列时按行业名升序，结果可复现。
2. **龙头**。热点行业内部，相对强弱 = 个股收益 − 该行业等权收益。
   相对强弱和近 `amount_window` 日日均成交额各自排名（高者秩大，并列取平均秩），两者相加。
   每个热点行业取得分最高的 `top_m` 只。得分并列时依次比相对强弱、成交额、代码升序。
3. **组合**。入选标的等权，权重和为 1。
   超过 `max_positions` 时按「更热的行业在前、行业内名次在前」截断。
   组内秩和行业人数有关，不拿来跨行业比大小。

成交额列缺失或全非正时，龙头只按相对强弱排序，并告警这一路没有生效。
成交额突增没有做成第二套热点定义：收益字段已经在行情里，两套口径并存却没有对照实验。

| 参数 | 默认 | 含义 |
|---|---|---|
| `top_k` | 5 | 热点行业数 |
| `top_m` | 2 | 每个热点行业的龙头只数 |
| `rebalance` | `daily_on_change` | 日频评估，目标相对当前持仓没变则不下单 |
| `window` | 20 | 行业收益与相对强弱的回看交易日 |
| `amount_window` | 20 | 日均成交额的回看交易日 |
| `min_industry_members` | 3 | 行业有效样本少于此数不参与排名 |

默认 5 × 2 = 10 只，每只权重 10%，与 `portfolio.max_weight_per_stock` 对齐。
入选少于 10 只时，回测引擎按单票上限把多出来的权重留成现金，不重新归一。

---

## 2. 股票池、调仓、仓位

- 股票池沿用 `config/universe.yaml`：沪深主板与创业板。
  科创板（688/689）和北交所不在 `allowed_prefixes` 里，引擎会把池外代码丢掉。
  策略本身不再写一套板块白名单，避免和配置各说各话。
- ST 与停牌由股票池按 status 表剔除。行情上如果带了 `is_st` / `is_suspended`，
  策略再用截至 as_of 的最后一条标记剔一次。更早的戴帽记录不参与，信号日已经摘帽的股票留在池内。
- **日频按需调仓**。每个交易日收盘都重新算目标，不读 `backtest.rebalance_frequency` 的月频。
  目标名单和权重与上次提交的一致，且当前持股名单已经是这批股票，则不下单。
  价格把市值权重带离等权时也不调。持股名单还没跟上（例如买入没成交）时，下一交易日继续调。
  命令行 `--freq` 改不了这条规则。
- 实盘冻结样本的季度调仓和 40% 仓位留在 `live_signal.py`。本策略不读 `config/live.yaml`，
  也不写入 `live/`。

---

## 3. 行业字段

| 顺序 | 字段 | 用法 |
|---|---|---|
| 优先 | `bars.industry` | 截至 as_of 的最后一条非空标签。有这个列就只用它，不用静态表把没标注的股票补进来 |
| 退路 | `industry_map`（来自 `instruments.industry`） | 当前静态分类。策略告警，回测只验证机制 |
| 都没有 | — | 返回空组合并告警。空仓表示缺数据 |

`scripts/fetch_industry.py` 写入的是 baostock 当前证监会行业，不是历史快照。
TODO：接入逐日行业历史之后，才能把这条规则放进 `reports/research.md` 的判定。

---

## 4. 可证伪断言

1. **给定构造行情，入选代码与等权权重等于手算。**
   锁定在 `tests/test_hotspot_dragonhead.py`。
   撤回条件：测试里的价格、成交额或行业标签被改了，或者排名规则改了却没改测试。
2. **缺行业时组合为空，并且告警进回测的 `validation_warnings`，全回测只出现一次。**
   撤回条件：有人把空仓读成看空。告警文本写的是缺 `bars.industry` 或 `industry_map`。
3. **静态行业上的收益不能当作这条规则有效或无效的证据。**
   撤回条件：`bars.industry` 是逐日快照，信号日只用 `trade_date <= as_of` 的标签，
   并且三段回测按 `reports/research.md` 第 10 节的三关重跑之后。在那之前，收益数字即使跑出来也撤回。
4. **as_of 之后的价格和行业标签不改变目标权重。**
   同一文件里有对应测试。撤回条件：该测试失败。
5. **目标名单连续多日不变时，成交只发生在第一次建仓；名单换了，下一交易日才再成交。**
   价格漂移本身不成交。撤回条件：`test_daily_on_change_holds_when_the_book_is_unchanged`
   或 `test_daily_on_change_trades_when_the_leader_changes` 失败。

---

## 5. 怎么跑

```bash
python scripts/run_baseline_backtest.py --data-dir data/processed \
    --strategy hotspot --start 2020-01-01 --end 2024-12-31 --benchmark 000852
```

`--window` 覆盖收益窗口。`--top-k`、`--top-m`、`--min-industry-members` 覆盖对应参数。
`--reverse` 和 `--industry-neutral` 会直接退出：这条规则没有反向开关，行业分组也不是可选项。
`--liquidity` 和 `--capacity-aum` 首版不生效，脚本会打出警告。

`scripts/live_auto.py` 不引用这个策略。

---

*回测结果不代表未来收益。本项目用于研究，不构成投资建议。*
