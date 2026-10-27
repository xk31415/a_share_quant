# 炒作苗头 + 观察池（paper / 观察仿真）

这是一条可回测的仿真规则，用来把晨报式「观察池大约 3 只、主线题材 1–3 条」收成可检验的记录。

它**不是买卖建议**，也**没有已验证收益**。
策略类 `paper_only` 必须为 true。回测只产纸面记录。
仿真账户的下单不走 `live_signal.py`，而走 `scripts/live_signal_hype.py`
（`live_auto.py` 仅当 `config/live.yaml` 的 `strategy` 为 `hype_watch` 时调用）。
跑出来的净值、回撤、换手和成本只描述这一次纸面路径，不要写进 `reports/research.md` 或 README 的研究结论。

板块：沪深主板 + 创业板。科创板（688/689）和北交所不进观察池，也不计入涨停家数。
ST、停牌沿用股票池：策略再按当日 `is_st` / `is_suspended` 剔除一次。`000300` 始终排除。
`000852` 同时是中证1000 和石化机械，不因为当基准就被剔出股票池。

---

## 怎么跑

```bash
# 纯规则：题材成员表 + 行情里的涨停价和后复权收盘价
python scripts/run_baseline_backtest.py --strategy hype_watch \
    --data-dir data/processed_2025_2026 \
    --start 2025-01-01 --end 2026-10-20 \
    --themes path/to/themes.csv

# 晨报名单。默认仍要过下面的涨停家数和超额门槛
python scripts/run_baseline_backtest.py --strategy hype_watch \
    --data-dir data/processed_2025_2026 \
    --themes path/to/themes.csv \
    --watchlist path/to/morning.csv

# 下一交易日改用收盘价撮合（默认开盘价）
python scripts/run_baseline_backtest.py --strategy hype_watch \
    --data-dir data/processed_2025_2026 \
    --themes path/to/themes.csv \
    --execution-price close
```

没有 `--watchlist` 时走纯规则。`input_mode=rules` 时如果还传了晨报 CSV，直接报错，避免名单被静默丢掉。
`--max-positions` 不能用来截断这个策略：观察池只数看 `pool_size`。等权截断会按代码字典序丢掉名次。

产物在 `results/EXP-*/`，除了原来的净值、成交、成本，还有：

| 文件 | 内容 |
|---|---|
| `signal_log.csv` | 每个信号日的题材评估和观察池。`mode=paper`，`label=paper/观察仿真` |
| `observations.csv` | 入选标的在 T+1/T+3/T+5 的后复权收盘收益。事后指标，窗口不够记 `pending`，缺行情记 `missing` |
| `paper_summary.json` | 期末净值、最大回撤、换仓次数、总成本、成本假设、断路参数。标明未验证 |
| `metrics.json` | 引擎原有指标，并带上 `paper_only` 和同一段声明 |

最大回撤与 `metrics.json` 同一口径（暖机截断，README 限制第 18 条）。
换仓次数 = 至少有一笔 `note=filled` 且 `shares>0` 的成交日个数，受阻单不算。

---

## 信号（T0 收盘才知道）

一条题材触发，要同时满足：

1. 当日可交易成分（股票池 ∩ 主板/创业板 ∩ 非 ST ∩ 非停牌）里，收盘价达到 `limit_up` 的家数 ≥ `min_limitup_count`（默认 3）。涨停价用行情里的 `limit_up`，不用 `pre_close × 10%`。有成分缺涨停价时，这一天该题材不触发（`missing_limit`），不会把缺数据当成「没涨停」。
2. 这些成分的后复权等权日收益 − 基准日收益 ≥ `min_theme_excess`（默认 0.01，即 1%）。基准默认中证1000（`000852`），从 `benchmark.csv` 读。当前数据集里没有沪深300（`000300`）。序列缺失就报错，不把这道门槛静默关掉。写成 `null` 才是明确关闭。
3. 能算出后复权日收益的成分不少于 `min_theme_members`（默认 3）。缺 `adj_close` 时超额这条直接报错，不改用原始收盘价。

触发的题材按涨停家数、超额、题材名排序，最多留 `max_themes` 条（1–3）。

观察池最多 `pool_size` 只（默认 3）：

- **纯规则**：题材内的涨停股按成交额从高到低、代码从小到大，再按题材轮转填满。
- **晨报 CSV 且 `csv_require_trigger: true`（默认）**：只用当日名单里的代码，而且它所在的题材必须已经触发。没触发的名字留在日志里，原因是 `theme_not_triggered`，权重为 0。
- **`csv_require_trigger: false`**：只回放当日名单，涨停家数和超额不生效。启动时会警告。这是在重放一份名单，不是在独立检验规则。

目标权重在 T0 收盘生成。T0 之后的行情、尚未开始的题材成员、晨报里更晚的日期，都不参与。

可证伪的说法：

- 可交易成分里的收盘涨停家数 < N，该题材不进观察池。
- 超额 < X，该题材不进观察池。
- 删掉 T0 之后的行情，目标权重不变。
- 688/689 和北交所前缀不出现在权重里，也不被算进涨停家数。
- `paper_only: false` 直接报错。

---

## 成交假设

引擎原有约定：T 日收盘出信号，**下一交易日**按 `execution_price` 撮合（`open` 或 `close`）。这就是 shift(1)。默认开盘价。

涨跌停看的是**同一根**成交价：开盘撮合时，开盘封板买不进；收盘撮合时，收盘封板买不进。没有盘中部分成交。

仓位：

- `equal`：入选标的平分 `gross_exposure`（默认 1，即纸面满仓分给观察池；不是建议满仓）。
- `fixed`：每只 `fixed_weight`。合计超过 `gross_exposure` 时缩到这个上限，不放大。

脚本把单票权重上限放到 1，避免 3 只等权被全局的 10% 上限截成 30% 仓位。观察池自己的上限是 `gross_exposure` 和 `pool_size`。

信号消失的下一交易日，目标为空，仿真会把持仓卖掉。观察窗口不是持有期。

---

## 成本

只在本策略里覆盖全局成本，不改 `costs:` 段，因此等权 / 动量 / 低波的默认成本不变。

| 项 | 默认 | 含义 |
|---|---|---|
| 佣金率 | 0.00025 | 双边 |
| 滑点率 | 0.00075 | 成交价向不利方向偏移 |
| 合计 | 0.001 | 单边比例摩擦约 0.10% |
| 最低佣金 | 5 元 | 小单上会高于 0.10% |
| 印花税 | 0.0005 | 仅卖出，不在这 0.10% 里 |

这些是配置，不是实测滑点。

---

## 断路

`circuit_breaker.enabled: true` 时：信号日收盘权益相对**近 `lookback` 个交易日峰值**的回撤 ≤ `max_drawdown`（默认 -0.08）则目标为空，并在随后的信号日继续空仓。`cooldown_days` 含触发当天，按**信号日**计数；默认日频时才等于 5 个交易日。频率改成周或月时，日志会警告这不是 N 个交易日。

窗口是滚动峰值。20 天前的高点如果已经滚出窗口，就不再算。历史高点从回测第一天算起的那种回撤，这里不算。

权益序列里晚于信号日的点会被丢掉。样本不足 `lookback` 天时记 `circuit_warmup`，不断路。`-8%` 要写成 `-0.08`，写成 `-8` 会报错。

---

## CSV

晨报 / 观察池 `morning.csv`：

```csv
trade_date,symbol,theme,rank
2025-03-03,600001,AI,1
2025-03-03,000001,AI,2
```

`rank` 可缺。代码必须是保留前导零的 6 位字符串，读成 `600001.0` 会报错，不会自动补零。同一 `(trade_date, symbol, theme)` 重复会报错。

题材成员 `themes.csv`：

```csv
symbol,theme,start_date,end_date
600001,AI,2024-01-02,2024-06-28
600002,AI,2024-02-01,
```

`start_date` 必填。空着表示「用现在的成分回填历史」，直接拒绝。`end_date` 空表示从 `start_date` 起仍在题材里，只向后有效。`end_date` 早于 `start_date` 会报错。

纯规则在没有这张表时，只用信号日 `bars` 上的 `theme` 列；没有 `theme` 才退到 `industry`，并告警。不读 `instruments` 里的静态行业，也不把全市场当成一条题材。

---

## 参数

全部在 `config/settings.yaml` 的 `hype_watch` 段。下表是当前默认值，改默认值要改测试里锁住的那一组，不要按收益搜索。

| 键 | 默认 | 作用 |
|---|---|---|
| `paper_only` | true | 必须为 true |
| `input_mode` | auto | 有晨报走名单，否则纯规则 |
| `pool_size` | 3 | 观察池只数，1–5 |
| `max_themes` | 3 | 主线条数，1–3 |
| `min_limitup_count` | 3 | 收盘涨停家数 |
| `min_theme_excess` | 0.01 | 相对基准的超额；`null` 关闭 |
| `min_theme_members` | 3 | 至少多少只算得出日收益 |
| `observe_horizons` | 1, 3, 5 | 事后观察，不参与下单 |
| `position_mode` | equal | `equal` 或 `fixed` |
| `gross_exposure` | 1.0 | 纸面总仓位上限 |
| `fixed_weight` | 0.20 | `fixed` 时每只的权重 |
| `execution_price` | open | 下一交易日的撮合列，`open` 或 `close` |
| `rebalance_frequency` | D | 信号频率。冷静期按这个频率的信号日计 |
| `benchmark` | 000852 | 超额用的指数（中证1000），读 `benchmark.csv` |
| `limitup_abs_tol` | 0.0001 | `close + tol >= limit_up` 视为涨停（元） |
| `csv_require_trigger` | true | false 时只回放名单 |
| `circuit_breaker.lookback` | 20 | 滚动交易日 |
| `circuit_breaker.max_drawdown` | -0.08 | 峰值回撤阈值 |
| `circuit_breaker.cooldown_days` | 5 | 含触发当日 |

已知限制见 README 第 30 条。
