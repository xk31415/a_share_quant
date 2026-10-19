# R01 Grok

## 元信息

- 角色：Grok
- 轮次：R01
- reviewed_commit：`758bce99b43accb14083c2f72be23307b8d626f3`
- 与 LEDGER 锚定 `3ca8edc` 的关系：HEAD 已前进 1 个提交（`758bce9` 建立审查协议并删除 `a_share_quant_REVIEW.md`）。`git diff --stat 3ca8edc HEAD -- src tests scripts config` 为空，下面引用的 `src/` 行在当前 HEAD 仍存在，条目不是 stale。
- 本轮范围：复核 `fix_log.md` §8 九项修复 / 回应 F-01～F-06 / 新增 F-07～F-14
- 验证方式：对当前工作树读代码，并用内存 `FakeProvider` / 临时 CSV / 临时 parquet 做可执行探针。未改 `src/`、`tests/`、`config/`、`scripts/`。

---

## 一、第八节九项修复复核

### 1. 策略可绕过股票池 —— 修对了

`BacktestEngine._enforce_universe` 把正权重与当日股票池求交，池外标的只进 `universe_violations`，不进 `pending_weights`。

探针：`Fixed({"688001": 1.0})`，`allowed_prefixes` 不含 `688`，8 个交易日。`trades` 为空，`universe_violations.symbol` 全部是 `688001`，`validation_warnings` 含「股票池外」。

未引入新的绕过。池外权重被丢掉之后没有重新归一，剩余权重合计小于 1、现金变多。这与 `build_target_weights`「不强行归一到满仓」同一口径，不单列发现。

### 2. 板块白名单 —— 修对了

`UniverseBuilder.get_universe` 用 `str.startswith(prefixes)`，再按 `excluded_symbols` 精确剔除。

探针（`min_list_days=0`，前缀与 `config/universe.yaml` 相同，`excluded_symbols=["000300"]`），输入：

`600001, 000001, 300001, 688001, 689001, 830799, 430001, 920001, 200001, 900001, 000300`

输出宇宙恰好是 `['000001', '300001', '600001']`。

白名单逻辑本身没有把科创板 / 北交所 / B 股放进来。`000016` / `000905` / `000852` 仍会入池，这是 F-03 的范围，不是本项把前缀写反。parquet 整型代码见 F-12。

### 3. 停牌日估值 —— 修对了

`engine.py:281-286`：

```text
px_mask = day_bars["close"].notna()
if "is_suspended" in day_bars.columns:
    px_mask &= ~day_bars["is_suspended"].astype(bool)
```

探针：2021-01-06 将 OHLC 与涨跌停价全部翻倍，同时 `status.is_suspended=True`。该日收益为 `0.0`，成交 `note` 为 `blocked:suspended`。停牌 bar 没有再进入 `last_prices`。

CSV 路径上 `is_suspended` 先经过 `coerce_bool`，字符串 `"False"` 不会被 `astype(bool)` 当成停牌。未看到估值退回「停牌日照用当日 close」。

### 4. 逐行涨跌停缺失 —— 修对了

`_require_limit_columns` 对非 `limit_exempt` 的空 `limit_up` / `limit_down` 直接 `DataValidationError`。

探针：单行 `limit_up=NaN` 且无豁免，`run()` 抛出「bars.limit_up 有 1 行为空值」。同一行 `limit_exempt=True` 且涨跌停价为 NaN、开盘价 15 时，该日有成交（33300 股）。

另用 CSV 把 `limit_exempt` 留空：`LocalFileProvider.get_bars` 读出的值是 `False`（`coerce_bool` 把空串映射为 False），不会变成豁免。未引入「留空即跳过涨跌停」。

### 5. 目标内缺行情留痕 —— 留痕有了，方向写错

不在目标里的持仓、成交日又没有 bar 时，卖出方向是对的：退市探针里 `direction=sell`、`note=blocked:no_bar`（见 F-02 补充）。

目标权重仍为正、但参考价缺失时，`generate_orders` 无条件记 `direction="buy"`。见 F-07。这是本项修复引进的。

### 6. 成交量上限 —— 有成交量的买单截断是对的，同时引进四条新问题

`volume=10000`、`max_participation_rate=0.10` 时，买单股数 ≤ 1000，且 `capped` 为真。这一条成立。

同时：

- `volume` 为 NaN 时上限被跳过（F-08）
- 卖出上限按 `lot_size=1` 截断，可以成交 250 股（F-09）
- 截断用的是成交日全日 `volume`，开盘价不变时成交股数随该列变化（F-10）
- 现金缩放也会把 `capped` 置真（F-13）

`fix_log.md` §8 写「默认 10%」。`settings.yaml:29` 是 `0.10`，`run_baseline_backtest.py:118` 会把配置传进引擎。`BacktestEngine.__init__` 的默认值是 `0.0`（`engine.py:92`）。`QUICKSTART.md:202-210` 的 `BacktestEngine(...)` 不传该参数。探针：`volume=100`、不传参，买入 10000 股；若按 10% 截断，上限 10 股，不足一手，应是 `blocked:volume`。基线脚本路径开了约束，库默认值和 QUICKSTART 路径没有。

### 7. `validation_warnings` 进产物 —— 引擎窗口内的写进了，全量校验没有

`run_baseline_backtest.py:169` 把 `result.validation_warnings` 写入 `metrics.json`，`:200-201` 写入 `ExperimentRecord`，同一次还写了 `industry_source`。引擎自己在 `_prepare_data` 里对回测窗口做过 `validate_dataset`，所以窗口内的警告会落盘。

`:83-87` 对全样本又做了一次校验，`:123` 合并进 `all_warnings` 只用于打印。落盘用的不是 `all_warnings`。窗口外的警告仍只出现在终端。见 F-14。

### 8. 流动性回看差一 —— 窗口边界修对了

旧代码是 `hist[len - lookback - 1]`，现为 `builder.py:206`：

```text
start = hist[max(0, len(hist) - lookback)]
```

探针：`lookback=3`、阈值 50。窗口前一天 `amount=10000`、窗口内每天 `amount=10` 的 `OLD` 不在宇宙里；窗口内三天的均值是 10，窗口外那天没有被算进去。差一没有回潮。

同一函数的分母仍是「有 bar 的行数」，不是 lookback 个交易日。见 F-11。这不是本项改边界引入的，是同一段里另一条未修的偏差。

### 9. CSV 前导零 —— CSV 修对了，parquet 整型没有

临时 `bars.csv` / `instruments.csv` 写入未加引号的 `000001`、`000300`。`LocalFileProvider` 读出的 `symbol` 仍是这两个字符串。白名单前缀 `000` 加 `excluded_symbols=["000300"]` 后，宇宙是 `['000001']`。

`loader.py:83-89` 只在 CSV 分支传了 `dtype={"symbol": str}`。parquet 整型见 F-12。

---

## 二、对上一轮条目的回应

### F-01 除权日持仓市值按原始收盘价腰斩

- 立场：接受现状
- 依据：估值写入的是原始 `close`（`engine.py:284-286`），`PortfolioState` 的股数只在 `buy` / `sell` 里变化，没有任何公司行动拆并。`schema.py:4-6` 规定 `adj_close` 只用于信号。探针：2021-01-07 把 OHLC 腰斩、`adj_close` 保持腰斩前的值、成本为 0。该日收益 `-0.49950099800399206`，前一日与当日持仓都是 10000 股。
- 新增断言：无

### F-02 卖单被拦截后不重试；退市持仓没有清算价

- 立场：接受现状，并补充证据
- 依据：成交尝试结束后 `pending_weights = None`（`engine.py:275`）。`max_positions` 只在 `build_target_weights` 里截断目标个数（`construction.py:30`），不限制成交后的实际持仓。
- 新增断言：月频、`max_positions=1`、初始资金 100000、2021-01-29 之前目标为 AAA 40%。2021-02-01（1 月信号的成交日）AAA 开盘等于跌停。结果只有一行 AAA 卖单：`blocked:limit_down`，同日用剩余现金买入 BBB 5700 股。2021-02-08 仍同时持有 AAA 3900 与 BBB 5700。`summarize` 的 `max_positions_held=2`。2 月 1 日之后、下一次月末信号之前没有第二笔 AAA 卖单。
- 退市补充：AAA 自 2021-01-06 起没有 bar，BBB 仍有 bar，目标改为只持有 BBB。AAA 每天一条 `sell / 0 / blocked:no_bar`，样本最后一日仍持有 AAA 10000 股，期末权益 `100100.0`（按最后一根 close 冻结，没有清算成现金）。

### F-03 无交易所后缀，前缀白名单只能靠 `excluded_symbols` 兜底

- 立场：接受现状，并补充证据
- 依据：`universe.yaml:15-29` 的白名单含前缀 `"000"`，`excluded_symbols` 只有 `"000300"`。匹配是 `startswith`，剔除是精确字符串。
- 新增断言：同一规则下，`000001, 000016, 000852, 000905, 600000` 入池，`000300` 与 `399001` 不入池。`000016` / `000905` / `000852` 没有被 `excluded_symbols` 接住。

### F-04 印花税不按成交日切换，过户费未建模

- 立场：接受现状
- 依据：`CostModel.stamp_duty_rate` 是单个浮点，默认 `0.0005`（`costs.py:19`）。`sell_cost` 的参数只有 `turnover`（`costs.py:46`），函数体在 `costs.py:47-51`，没有成交日。`100000` 元卖出的印花税部分是 `50.0`，即 5 bps。源码中没有过户费项。2023-08-28 之前的 10 bps 不会被用到。
- 新增断言：无

### F-05 等权按代码字典序截断

- 立场：接受现状
- 依据：`equal_weight.py:32` 为 `picked = sorted(universe)[: self.max_positions]`。
- 新增断言：宇宙 `["600002", "600001", "300001", "000001"]`、`max_positions=2`，权重是 `000001=0.5`、`300001=0.5`。`600001` 与 `600002` 被截掉。

### F-06 基准序列已加载，指标层不算超额

- 立场：接受现状
- 依据：`_load_benchmark`（`engine.py:419-432`）在 bars 里能找到基准时返回价格序列。`summarize`（`performance.py:241-294`）的键里没有超额收益、信息比率、跟踪误差。
- 新增断言：bars 含 `000300` 且 `BacktestConfig.benchmark="000300"` 时，`result.benchmark.name` 为 `000300`、长度为 6。`summarize` 里名称含 `excess` / `information` / `tracking` / `alpha` / `benchmark` 的键为空。

---

## 三、新增发现

reviewed_commit：`758bce99b43accb14083c2f72be23307b8d626f3`（下列行号在该提交上核对过）

### F-07 目标内缺参考价时把减仓记成 0 股买单，卖单不生成

- 主张：成交日参考价缺失且目标权重仍为正时，`generate_orders` 固定追加 `Order(sym, "buy", 0, reason="blocked:no_bar")`，不生成减仓卖单，持仓股数保持不变。
- 证据：`src/a_share_quant/backtest/execution.py:73-76`

```text
if price is None or price <= 0:
    # 目标里有、但成交日没有行情（退市/长期停牌）——必须留下记录，
    # 不能 continue 掉，否则 trades.csv 里看不出"本来该买/该卖"。
    buy_orders.append(Order(sym, "buy", 0, reason="blocked:no_bar"))
```

- 复现：月频，初始资金 100000，成本为 0。2021-01-29 之前目标 `{"AAA": 1.0}`，当日及之后目标 `{"AAA": 0.01, "BBB": 0.99}`。删掉 2021-02-01 的 AAA bar，BBB 当天有 bar。AAA 在 2021-01-29 的 close 为 `10.4392`。实际成交只有两行：`2021-01-01 AAA buy 9700 filled`，`2021-02-01 AAA buy 0 blocked:no_bar`。没有 AAA 的 sell。2021-02-01 与 2021-02-10 的持仓都是 AAA 9700 股。9700×10.4392=101260.24。按这笔持仓市值，权重 0.01 对应 97 股；总权益不低于该持仓市值，目标股数仍远小于 9700。实际卖出股数为 0。
- 失败场景：调仓成交日该标的缺 bar（未标停牌、策略仍给出正权重）。持仓维持在缺口前的股数，直到下一次信号日才有机会再平衡。`trades.csv` 把这次缺口记成买单失败。
- 严重度：中
- 撤回条件：参考价缺失且该标的仍在正权重目标中时，若生成的是卖出方向的拦截，或持仓被减到与目标股数一致，则本条不成立。标的不在目标中、走清仓分支并记 `sell / blocked:no_bar` 的情形不属于本条。

### F-08 `volume` 为 NaN 时成交量上限静默关闭

- 主张：`max_participation_rate>0` 时，行上 `volume` 缺失或为 NaN，`_volume_cap` 返回 `None`（不限制）。该行按没有上限成交，`capped` 为 False。
- 证据：`src/a_share_quant/backtest/execution.py:134-136`

```text
vol = row.get("volume")
if vol is None or pd.isna(vol):
    return None
```

`validate_bars` 把 NaN 收成警告而不是错误（`src/a_share_quant/data/validation.py:53-55`）：

```text
for col, n in df[list(BAR_REQUIRED_COLUMNS)].isna().sum().items():
    if n:
        issues.append(f"column {col!r} has {int(n)} NaN values")
```

- 复现：全部 `volume=NaN`，`max_participation_rate=0.10`，价格 10，资金 1000000，目标权重 1。`run()` 不抛异常。买入 `100000` 股，`note=filled`，`capped=False`。按 10% 上限这行没有可用的成交量，不应出现 100000 股的成交。
- 失败场景：数据源成交量为空、配置又开启了 `max_participation_rate`。回测继续，成交规模与「未设上限」相同，容量约束失效的方向是成交过多。
- 严重度：高
- 撤回条件：`volume` 为 NaN 或缺失且 `max_participation_rate>0` 时，若该笔被拒绝或成交股数为 0，则本条不成立。`volume` 有值时上限生效，不属于本条。

### F-09 卖出成交量上限不按整手取整

- 主张：卖出调用 `_volume_cap(row)` 时使用默认 `lot_size=1`，截断后的股数可以不是 100 的整数倍，也不是「把剩余零股一次卖光」。买入路径传入 `self.lot_size`，两边不一致。
- 证据：`src/a_share_quant/backtest/execution.py:130` 与 `:186`

```text
def _volume_cap(self, row, lot_size: int = 1) -> Optional[int]:
```

```text
cap = self._volume_cap(row)
```

对照买入（`execution.py:226-227`）：

```text
eligible.append((od, self.cost_model.buy_price(open_px),
                 self._volume_cap(row, self.lot_size)))
```

- 复现：持仓 1000 股且当日可卖，卖出委托 1000，`volume=2500`，`max_participation_rate=0.10`，`lot_size=100`。`int(2500*0.10)=250`。实际成交 `sell 250 filled capped=True`，剩余 750 股。250 不是 100 的整数倍；750 也不是把零股一次清掉。同一上限若走买入的整手取整，是 `200`。
- 失败场景：卖出委托大于 `volume * rate`，且该乘积向下取整后不是一手的整数倍。账上会出现交易所委托规则不允许的股数，后续按整手计算的减仓不会把这个零股对齐回去，直到一笔清仓卖单。
- 严重度：中
- 撤回条件：卖出截断股数始终是 `lot_size` 的整数倍，或截断结果等于当时的全部可卖股数，则本条不成立。

### F-10 开盘撮合的成交量上限使用成交日全日成交量

- 主张：撮合价是成交日开盘价，成交股数上限使用同一行的全日 `volume`。开盘价不变时，改全日 `volume` 会改变成交股数。
- 证据：时间对齐在 `engine.py:9` 与 `:51`

```text
* 目标权重于 d 收盘生成，d+1（下一交易日）开盘撮合；
```

```text
execution_price: str = "open"   # t+1 成交参考价列名
```

上限取自该行 `volume`（`execution.py:137`）：

```text
cap = int(float(vol) * self.max_participation_rate)
```

参考价来自当日 bar 的 `execution_price` 列（`engine.py:240-241`）：

```text
day_bars.set_index("symbol")[cfg.execution_price]
```

- 复现：价格路径相同，`max_participation_rate=0.10`，资金 1000000，只改成交日 `volume`。`volume=1000` 时该日买入 100 股；`volume=10000000` 时买入 100000 股。两行的开盘价同为构造器给出的开盘价。
- 失败场景：开盘能成交的数量被全日（含开盘之后）的成交量放大或缩小。全日量越大，开盘撮合越宽松。
- 严重度：中
- 撤回条件：若 `max_participation_rate` 的定义就是「用成交日全日成交量做事后容量筛选」，而不是开盘时点已知的可成交数量，则本条不成立。

### F-11 流动性日均成交额的分母是有 bar 的行数

- 主张：`min_avg_daily_amount` 的均值是窗口内实际行情行的 `amount` 均值。窗口内没有 bar 的交易日不进入分母，单日放量可以通过高于「按 lookback 天平均」的阈值。
- 证据：`src/a_share_quant/universe/builder.py:206-212`

```text
start = hist[max(0, len(hist) - lookback)]
bars = self._provider.get_bars(start, ts)
if not bars.empty:
    avg = bars.groupby("symbol")["amount"].mean()
    candidates = [
        s for s in candidates
        if s in avg.index and float(avg.loc[s]) >= rules.min_avg_daily_amount
```

- 复现：5 个交易日，`lookback=5`，阈值 200。`FULL` 每天 `amount=100`，不在宇宙中。`GAP` 只在最后一天有 bar 且 `amount=600`，宇宙为 `['GAP']`。`GAP` 的观测均值是 600；若缺的 4 天按 0 计入，均值为 120，低于 200。窗口边界另测：`lookback=3` 时，窗口前一天 `amount=10000`、窗口内为 10 的标的不入池（差一修复仍然成立，与本条不是同一断言）。
- 失败场景：停牌或数据缺口导致窗口内少 bar、但有 bar 的日子成交额很高。该标的比「每个交易日都有 bar、成交额稳定但较低」的标的更容易通过流动性过滤。
- 严重度：中
- 撤回条件：缺 bar 的交易日如果仍有 `amount=0` 的行情行，该日会进入均值，本条不成立。本条只覆盖窗口内交易日没有 bar 的数据。

### F-12 parquet 里的整型 `symbol` 前导零仍然丢失

- 主张：CSV 已按字符串读取；parquet 分支直接 `read_parquet`。整数 `1` 经过 `astype(str)` 变成 `"1"`，不再匹配前缀 `"000"`。bars 与 instruments 都是这种整数时，两边都变成 `"1"`，校验不会因为代码对不上而报错，白名单会把该证券滤成空宇宙。
- 证据：`src/a_share_quant/data/loader.py:83-89`

```text
if path.suffix == ".parquet":
    df = pd.read_parquet(path)
else:
    df = pd.read_csv(path, dtype={"symbol": str})
```

`coerce_bars` 对字符串列只做 `astype(str)`（`schema.py:112-113`）：

```text
elif dtype == "object":
    out[col] = out[col].astype(str)
```

- 复现：bars、instruments、status 都写成 parquet，`symbol` 列为整数 `1`。读出的 bars 与 instruments 都是 `['1']`。`allowed_prefixes=["000","600"]` 时宇宙为 `[]`。不设白名单时宇宙为 `['1']`。对照：CSV 中的 `000001` 读入后仍是 `000001`（第八节第 9 项）。
- 失败场景：行情以整型代码写入 parquet（前导零在写入时已经没有了）。板块白名单把本应属于 `"000"` 的证券全部剔除；关掉白名单时，代码变成 `"1"`，与任何带前导零的表对不上。
- 严重度：中
- 撤回条件：parquet 的 `symbol` 已是保留前导零的字符串，读入后仍为 `000001`，则本条不成立。本条只覆盖整型（或其它会变成 `"1"` 的数值类型）写入 parquet 的情形。

### F-13 现金缩放被记成成交量截断

- 主张：买单的 `capped` 在 `cap is not None` 且最终股数小于委托股数时为真。现金缩放把股数降到低于委托时，即使股数仍小于成交量上限，`capped` 也是 True。`summarize` 的 `n_capped_trades` 计的是这一列。
- 证据：`src/a_share_quant/backtest/execution.py:256-257`

```text
fills.append(Fill(od.symbol, "buy", shares, price, cost, "filled",
                  capped=(cap is not None and shares < int(od.shares))))
```

- 复现：直接调用 `ExecutionSimulator.execute`。现金 5000，价格 10，委托买入 2000 股，`volume=1000000`，`max_participation_rate=0.10`，成本为 0，`lot_size=100`。成交量上限为 100000 股。实际成交 400 股，`note=filled`，`capped=True`。400 < 100000，绑定约束是现金，不是成交量。
- 失败场景：开启了成交量上限、同时现金不够按委托全额买入。`n_capped_trades` 把现金不足造成的缩量算进成交量截断，容量约束看起来比实际更常触发。
- 严重度：低
- 撤回条件：`cap is None`，或最终股数小于委托的唯一原因是 `shares` 被成交量上限压低（现金足够按该上限买入），则本条不成立。

### F-14 全样本校验警告被打印，不写入产物

- 主张：`run_baseline_backtest.py` 落盘的 `validation_warnings` 是引擎回测窗口的 `result.validation_warnings`。脚本对全样本调用 `validate_dataset` 得到的 `warnings` 只进入打印用的 `all_warnings`。窗口外才有的警告不在 `metrics.json` 和 experiment JSON 里。
- 证据：`scripts/run_baseline_backtest.py:123` 与 `:169`

```text
all_warnings = list(dict.fromkeys(list(warnings) + list(result.validation_warnings)))
```

```text
report["validation_warnings"] = list(result.validation_warnings)
```

引擎窗口由 `engine.py:206` 决定：

```text
bars, warnings = self._prepare_data(trading_days[0], trading_days[-1])
```

- 复现：6 个交易日的 bars，只把第一天的 `volume` 设为 NaN。对全样本 `validate_dataset` 的返回值含 `column 'volume' has 1 NaN values`。对从第 4 个交易日起的子样本调用同一函数，返回值里没有这条 volume 警告（两边都有幸存者偏差那条）。脚本打印的是两者的并集，写入 `report["validation_warnings"]` 的是窗口那份。本次没有跑会写 `results/` 的基线脚本；上述两个列表就是该脚本第 83 行和第 169 行分别使用的输入。
- 失败场景：`--start` / `--end` 裁掉了含 NaN 的日期。终端看得到全样本警告，归档的 `metrics.json` 里没有。只读产物会把窗口外的数据问题当成不存在。
- 严重度：低
- 撤回条件：全样本警告与回测窗口警告的文本集合相同，则落盘与终端一致，本条不成立。窗口内的警告已写入 `result.validation_warnings`，不属于本条。

---

## 四、结论

- 第八节九项里：股票池求交、板块白名单、停牌日估值、逐行涨跌停、流动性窗口边界、CSV 前导零，按探针成立。缺行情留痕的方向、成交量上限的四条边界、全样本警告落盘，不成立或只成立一部分。
- 本轮新增 8 条：F-07、F-08、F-09、F-10、F-11、F-12、F-13、F-14。撤回 0 条。
- F-01～F-06 均接受现状。F-02、F-03 有补充断言。没有「认为不成立」的条目。
- C-01～C-12 没有新的可推翻断言，不重启。
- 仍待 Claude 判定的条目：F-01～F-14。
