# R03 Grok

reviewed_commit: 17dc3f24cb6a8e16959c1ab94107075b26b1d2f5

HEAD 与该 SHA 一致（`17dc3f24cb6a8e16959c1ab94107075b26b1d2f5`）。A/B 组七个改动都在这一笔：`17dc3f2 修复交叉审查确认的 A/B/C 组问题`。下面引用的行在该 commit 上仍存在。

现有测试：

```text
python3 -m pytest --override-ini addopts= -q --tb=no
106 passed in 1.72s
```

---

## 逐条复验

### F-07 已堵住

缺行情且已持仓时记 0 股卖单；未持仓记 0 股买单。减仓场景与 R01 的撤回条件一致。

证据：`src/a_share_quant/backtest/execution.py:78-81`

```text
if cur_held is not None and cur_held.shares > 0:
    sell_orders.append(Order(sym, "sell", 0, reason="blocked:no_bar"))
else:
    buy_orders.append(Order(sym, "buy", 0, reason="blocked:no_bar"))
```

复现：周频，初始资金 100000，成本为 0。2021-01-29 之前目标 `{"AAA": 1.0}`，之后 `{"AAA": 0.01, "BBB": 0.99}`，删掉 2021-01-18 的 AAA bar。

```text
trade_date symbol direction  shares           note
2021-01-18    AAA      sell       0 blocked:no_bar
```

未持仓、目标内缺 bar：`direction=buy shares=0 note=blocked:no_bar`。

附注（不另立）：同一缺口若目标是从 0.2 加到 1.0，这天仍记 `sell 0 blocked:no_bar`，下一根有行情的成交日才是 `buy 7900 filled`。0 股不改持仓。这与本次提交写明的「已持仓记 sell」一致，不是静默成交。

### F-08 已堵住

启动报错只在 `max_participation_rate > 0` 且窗口内 `volume` 有空值时触发。`=0` 与引擎不传该参数（默认 0.0）不报错。`>0` 且缺列或缺值时报错；执行层对 NaN 记 `blocked:no_volume_data`，不再按无上限成交。

证据：`src/a_share_quant/backtest/engine.py:135-139`

```text
if self.max_participation_rate <= 0:
    return
n_na = int(bars["volume"].isna().sum())
if n_na:
    raise DataValidationError(
```

调用点在 `engine.py:116` 的 `if self.validate:` 内，`engine.py:125` `self._require_volume_for_cap(bars)`。执行层：`execution.py:149-150` 缺量返回 `-1`，卖出 `execution.py:203-205`、买入 `execution.py:247-249` 记 `blocked:no_volume_data`。

复现（6 个交易日，`600000`，资金 1000000，目标权重 1，日频）：

| 输入 | 输出 |
|---|---|
| `volume` 全 NaN，`max_participation_rate=0.10` | `DataValidationError: max_participation_rate=0.1 已启用成交量上限，但 bars.volume 有 6 行为空值...` |
| 仅首行 NaN，rate=0.10 | 同上，`有 1 行为空值` |
| 去掉 `volume` 列，rate=0.10 | `DataValidationError: bars missing required columns: ['volume']` |
| `volume` 全 NaN，rate=0.0 | 不抛异常，买入 `[99000]`，`note=filled` |
| `volume` 全 NaN，不传 rate | 不抛异常，买入 `[99000]` |
| `volume=1e6`，rate=0.10 | 不抛异常；首笔 99000、`capped=False`（上限 100000，未绑定） |
| 直接 `execute` 买 100000，volume=NaN，rate=0.10 | `shares=0 note=blocked:no_volume_data capped=False` |
| 同上，rate=0 | `shares=99800 note=filled capped=False` |

`validate=False` 且 rate=0.10、volume 全 NaN：不抛启动异常，逐笔 `buy 0 blocked:no_volume_data`。不再出现 R01 的「99000 股照买、capped=False」。该开关同时跳过涨跌停缺列等启动校验，不另立。

### F-09 原断言已堵住；清仓在上限不绑定时不误伤

整手持仓、部分减仓被成交量上限截断时，成交股数改为 100 的整数倍。R02 的断言（持仓 1000、卖 500、volume=2500、rate=0.10 → 250 股、余 750）不再成立。

证据：`src/a_share_quant/backtest/execution.py:151-152` 与 `:202`

```text
cap = int(float(vol) * self.max_participation_rate)
return max(cap // lot_size * lot_size, 0)
```

```text
cap = self._volume_cap(row, self.lot_size)
```

复现：成本与滑点为 0，`lot_size=100`，`max_participation_rate=0.10`，持仓 1000 且当日可卖，委托卖 500，`volume=2500`。`int(2500*0.10)=250`，整手后上限 200。

```text
round-partial: shares=200 note=filled capped=True left=800
```

清仓路径（订单层）不把零股向下取整。`execution.py:66-67` 持仓不在目标中时 `Order(sym, "sell", int(pos.shares))`；`execution.py:91-92` 在 `desired == 0` 时同样卖出 `int(cur_shares)`。

持仓 1050、权益 100000、价格 10：

```text
target {}                         [('sell', 1050, '')]
target weight 0                   [('sell', 1050, '')]
target weight 0.001 (desired 0)   [('sell', 1050, '')]
```

这三笔委托再执行：rate=0，或 rate=0.10 且 `volume=1_000_000`（上限 100000 ≥ 1050），成交都是 `shares=1050 left=0`。纯零股 50 在 rate=0 或成交量充足时也是 `shares=50 left=0`。目标为 0 不会在订单层被取成 1000。

部分卖出仍走改动前的公式，`execution.py:94`：

```text
cut = (cur_shares - desired) // lot_size * lot_size
```

同一持仓 1050：目标权重 0.10（目标股数 1000）委托列表为 `[]`，50 股差额不成交，持仓保持 1050；目标权重 0.05（目标股数 500）委托 `sell 500`。不足一手的零股留在剩余持仓里，直到目标变为 0 才一次清掉。这行不在 `17dc3f2` 的 diff 里，不另立。

成交量上限把「原始上限已经够卖完全部零股」再向下取整，会留下残仓。见 F-15。这一点未堵住。

### F-11 已堵住

分母从「有 bar 的行数」改为窗口内交易日数。单日放量、其余交易日缺 bar 的均值下降，更难过阈值。没有改用 252 或 365。`metrics/performance.py:12` 的 `TRADING_DAYS_PER_YEAR = 252` 不在这笔 diff 里，年化与换手口径未动。

证据：`src/a_share_quant/universe/builder.py:207-212`

```text
start = hist[max(0, len(hist) - lookback)]
n_days = max(1, len(hist) - max(0, len(hist) - lookback))
bars = self._provider.get_bars(start, ts)
...
total = bars.groupby("symbol")["amount"].sum()
avg = total / n_days
```

`get_bars` 是闭区间（`loader.py` 的 `>= start` 且 `<= end`）。`n_days = min(lookback, len(hist))`，与 `[start, as_of]` 的交易日个数相同。

复现：

1. 5 个交易日，lookback=5，阈值 200。`FULL` 每日 amount=100，`GAP` 仅末日 600，`STEADY` 每日 200，`SPIKE` 仅末日 1000。宇宙为 `['SPIKE', 'STEADY']`。`GAP` 旧均值 600（会入池），新均值 600/5=120（不入池）。`STEADY` 仍为 200，入池。方向是缺 bar 的标的更难入池；每日都有 bar 的均值与旧的行均值相同。
2. 差一：`EXACT` 五日每日 200 入池，`LOW` 五日每日 199 不入池。分母是 5。若是 6，`EXACT` 的均值为 1000/6，不入池；若是 4，`LOW` 的均值为 995/4，入池。
3. 6 个交易日（2021-01-04 … 01-08、01-11），lookback=5，阈值 200。只在首日放量 10000 的 `ONLY_D0` 不入池；只在次日 1000 的 `ONLY_D1` 入池；只在末日 1000 的 `ONLY_LAST` 入池。窗口是末日往前恰好 5 个交易日，含两端，不含窗口前一日。
4. 只有 3 个交易日、lookback=20、末日一笔 amount=600。阈值 200 入池，250 与 300 都不入池。这是 600/3=200，不是 600/1=600，也不是 600/20=30。历史短于 lookback 时分母是窗口里实际有的交易日数。

### F-12 已堵住

parquet 的 `symbol` 为数值类型时直接报错。字符串 parquet 仍保留前导零。

证据：`src/a_share_quant/data/loader.py:87-91`

```text
if "symbol" in df.columns and pd.api.types.is_numeric_dtype(df["symbol"]):
    raise ValueError(
        f"{path.name} 的 symbol 列是数值类型 "
        f"({df['symbol'].dtype})，A 股代码的前导零已丢失"
        f"（000001 会变成 1）。请把 symbol 写成字符串后重新导出。")
```

复现：

```text
int symbol: bars.parquet 的 symbol 列是数值类型 (int64)，A 股代码的前导零已丢失（000001 会变成 1）。请把 symbol 写成字符串后重新导出。
float symbol: bars.parquet 的 symbol 列是数值类型 (float64)，...
string parquet symbols: ['000001'] dtype str
```

### F-13 已堵住

`capped` 只在委托股数大于成交量上限时为真。现金不足或整手缩量不再记成成交量截断。

证据：`src/a_share_quant/backtest/execution.py:267` 与 `:283`

```text
volume_bound = cap is not None and int(od.shares) > cap
```

```text
capped=volume_bound
```

复现（rate=0.10，`lot_size=100`，价格 10，成本 0）：

| 输入 | 输出 |
|---|---|
| 现金 5000，买 2000，volume=1e6（上限 100000） | `shares=400 capped=False` |
| 现金 1e6，买 5000，volume=2500（原始上限 250，整手 200） | `shares=200 capped=True` |
| 现金 5000，买 5000，volume=3000（上限 300，现金够买这个上限） | `shares=300 capped=True` |
| 现金 1e6，买 250，volume=1e6 | `shares=200 capped=False`（缩量来自整手） |
| rate=0，现金 5000，买 2000 | `shares=400 capped=False` |

R01 的「现金缩量却 `capped=True`」不再出现。成交量上限真正绑定时仍为真。

### F-14 已堵住

全样本才有的告警写入 `metrics.json` 和 experiment JSON。窗口内告警仍在合并结果里。

证据：`scripts/run_baseline_backtest.py:123`、`:169`、`:201`

```text
all_warnings = list(dict.fromkeys(list(warnings) + list(result.validation_warnings)))
```

```text
report["validation_warnings"] = list(all_warnings)
```

```text
validation_warnings=list(all_warnings),
```

复现：8 个交易日的 `600000`，只把 2021-01-04 的 `volume` 设为 NaN；回测窗口 2021-01-07～2021-01-13；`settings.yaml` 的 `max_participation_rate=0.10`（窗口内没有 NaN，引擎不因此启动失败）。

```text
python3 scripts/run_baseline_backtest.py \
  --data-dir /tmp/r03_f14/data \
  --universe-config /tmp/r03_f14/universe.yaml \
  --start 2021-01-07 --end 2021-01-13 --freq D \
  --out /tmp/r03_f14/out
```

终端：

```text
[warn] column 'volume' has 1 NaN values
```

同一函数对全样本的返回值含这条 volume 告警，对窗口的返回值不含。`metrics.json` 的 `validation_warnings` 含这条，且 `validation_warnings_source` 为 `full_sample + backtest_window（已去重）`。experiment JSON 的 `validation_warnings` 与 metrics 这份相同。

---

## 新增发现

### F-15 卖出成交量上限按整手向下取整，会截断本可一次成交的零股清仓

- 主张：卖出调用 `_volume_cap(row, self.lot_size)` 后，上限一律是 `lot_size` 的整数倍。当委托是含零股的一次性清仓，且 `int(volume * max_participation_rate)` 已经大于等于委托股数，但向下取整后的上限小于委托股数时，成交被改成更小的整手，或因上限变成 0 而被 `blocked:volume`，持仓留下零股。
- 证据：`src/a_share_quant/backtest/execution.py:151-152`

```text
cap = int(float(vol) * self.max_participation_rate)
return max(cap // lot_size * lot_size, 0)
```

`execution.py:202` `cap = self._volume_cap(row, self.lot_size)`；`execution.py:207-212` 在 `shares > cap` 时把成交股数改成该上限，上限为 0 则 `blocked:volume`。

- 复现：成本与滑点为 0，`lot_size=100`，`max_participation_rate=0.10`，持仓当日可卖。直接 `ExecutionSimulator.execute`。
  1. 持仓 1050，委托卖 1050，`volume=10500`。`10500*0.10=1050.0`，整手后上限 1000。实际 `shares=1000 note=filled capped=True left=50`。`volume=10990` 时原始上限 1099，整手后仍是 1000，同样 `left=50`。同一委托在 `volume=1_000_000` 时 `shares=1050 left=0`。
  2. 持仓 50，委托卖 50，`volume=800`。`800*0.10=80.0`，整手后上限 0。实际 `shares=0 note=blocked:volume capped=False left=50`。同一委托在 `volume=1_000_000` 时 `shares=50 left=0`。
  3. 持仓 150，委托卖 150，`volume=1800`。原始上限 180，整手后 100。实际 `shares=100 note=filled capped=True left=50`。
  订单层不是原因：`generate_orders({}, ...)` 对持仓 1050 给出 `sell 1050`；再拿到 `volume=10500` 上执行，仍是 `shares=1000 left=50`。
- 失败场景：持仓不是 100 股的整数倍，且 `max_participation_rate>0`，当日 `int(volume * rate)` 落在「大于等于清仓股数、小于下一个整手边界」的区间。50 股的清仓会在原始上限为 1～99 时整笔被拦；1050 股的清仓会在原始上限为 1050～1099 时留下 50 股。整手持仓的部分减仓不受影响（持仓 1000、卖 500、volume=2500 仍成交 200、余 800）。
- 严重度：中
- 撤回条件：上述输入 1 的成交股数为 1050、输入 2 的成交股数为 50，且对应持仓为 0。`max_participation_rate<=0` 时上限不生效，不在本条内。原始上限本身小于委托股数时留下的残仓是容量约束，不在本条内。

---

## 结论

- 本轮新增 1 条（F-15），撤回 0 条。
- F-07、F-08、F-11、F-12、F-13、F-14 已堵住。F-09 的原断言已堵住；清仓卖零股在上限不绑定或关闭时不误伤，在 F-15 的区间内未堵住。
- 无其他新增。

本文件只涉及审查流程，不构成投资建议。
