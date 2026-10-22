# R08 Grok 审查（回归：R07 修复、实盘层、回测约束、动量重跑）

- 角色：Grok
- 轮次：R08
- reviewed_commit: `1dfa16786c417713ce6c7355f2457470580530b0`
- 与 HEAD 是否一致：是。`git fetch origin main` 之后 `origin/main` 与本地 HEAD 都是 `1dfa167`（`docs: 补记动量重跑与待办盘点`）。下面引用的行在这个 SHA 上。
- 环境：Linux，CPython 3.12.3，pandas 3.0.6，numpy 2.5.4，pyarrow 26.0.0，pytest 9.1.1。探针在 `/tmp/r08_probe` 与 `/tmp/r08_probe2`，没有写入 `live/`，没有跑 `build_dataset.py` / `update_dataset.py`，没有跑多段完整回测。
- pytest：`python3 -m pytest --override-ini='addopts=' --tb=line -q` → **253 passed in 4.52s**（退出码 0）。

## 总评

R07 的保护在它测过的路径上还在：同一价格的明细与汇总指纹相同；带 `--save-holdings` 的同日重跑退出码 1，`reconcile.json` 的 `n_holdings_diff` 仍是 0。跨进程 `hash_pandas_object` 稳定（默认 hash key 是常量 `0123456789123456`）。

还能绕过去的是另一类输入。两笔不同成交价聚合成的均价，和券商按两位小数写出的汇总价，指纹不同，股数被再加一遍（F-36）。`--as-of` 换成另一天，同一份成交可以连加三次（F-37）。不传 `--save-holdings` 时指纹检查根本不跑，正确报告被按重复入账的偏离盖掉，退出码还是 0（F-38）。不传 `--holdings`、当天又只有买入时，已有持仓文件被收成「只有这一笔买单」，退出码 0，没有警告（F-39）。

回测引擎里，除权日持有的收益是 0，这和 F-01 的测试一致；把仓卖掉时现金按原始价乘未调整股数入账，10 送 10 的卖出日收益是 -50%，5% 现金分红的卖出日收益是 -5%（F-40）。退市强平在 `trades` 里记成 0 股（F-41）；退市日当天还有行情时，强平之后又按上一信号把同一只买回来（F-42）。

`397abfb` 没有改 `.py`。动量的 `iloc` 跳过窗口挑对了标的。这个提交留下的回归在文档：`strategy_log.md` §12.5 仍写「动量未重跑」并印着旧的 -22.86% / -25.56% / -9.26%，同文件 §11.1 已经换成 -14.90% / -16.09%（F-45）。`--max-positions` 不进入动量的 `top_n`，名额不够时按代码字典序截断（F-44）。非季度月的判断用的是回滚之后那个交易日的月份（F-43）。

## 已核对、不新编号

- 同价明细（100 = 60+40 @10.00）与汇总指纹相同；汇总价写成 `round(3)` 之后的 10.015 时，重跑退出码 1。
- 带 `--save-holdings` 的同键重跑：退出码 1，`h2` 不存在，`n_holdings_diff` 保持 0。这是 F-34 修过的那条路径。
- 成交代码 `1` 对委托 `000001`：退出码 2，不写持仓。前导零丢失在这条路上被「对不上单」挡住了，没有静默加仓。
- 删 `.digest` 后重跑仍会入账。这是 R07 任务书里的洞 ②，修复改的是提示语，不是「文件不在就拒绝」。不新编号。
- 指纹只记最后一批。`h0 -A→ h1 -B→ h2` 之后用 h2 重跑 A 仍然不拦。限制清单第 32 条已经写了。不新编号。
- `TopNMomentum(window=5, skip=2)` 在构造数据上选中了跳过窗口内上涨的那只，没有选到最后一天才跳的那只。
- 停牌估值、涨跌停缺列即报错、宇宙外不下单、流动性分母用交易日数，这几处读过当前实现，没有新的可证伪失败。F-10 全日成交量口径仍是当时记进文档的条件，不重开。

## 发现

### F-36 不同成交价的明细，对上两位小数的汇总价，指纹认不出，股数再加一遍

- 位置：`scripts/reconcile.py:119`
- 该行代码原文：`        "price": agg["price"].astype("float64").round(3),`
- 可证伪断言：同一只股票两笔买入 100 股 @10.01 与 100 股 @10.02，加权价是 10.015，`round(3)` 后指纹按 10.015 记。把同一批写成一行 `200 @ 10.02`（两位小数的均价）再对已经入过账的持仓，退出码是 0，股数从 1200 变成 1400。写成一行 `200 @ 10.015` 时退出码是 1，持仓文件不存在。
- 复现方式：临时目录，委托 `600000,buy,200,10.02`，起始持仓 `600000,1000`。第一份成交是两行 `100,10.01` 与 `100,10.02`，`--save-holdings` 得到 `600000,1200`，digest 前缀 `2026-10-20|4226d182`。第二份成交一行 `200,10.02`，`--holdings` 用上一份输出，退出码 0，新文件是 `600000,1400`。第三份把价格改成 `10.015`，退出码 1，文件未创建。同价拆单 `60+40 @10.00` 与一行 `100 @10.00` 的指纹相同（`7244fc5f99a3`），这条旧断言仍然成立。
- 建议档：A
- 撤回条件：`200 @ 10.02` 这次重跑退出码 1，且持股停在 1200。

### F-37 `--as-of` 换一个日期字符串，同一份成交可以连加三次

- 位置：`scripts/reconcile.py:198` 与 `:279`
- 该行代码原文：
  - `scripts/reconcile.py:198` `    as_of = args.as_of or _infer_as_of(base)`
  - `scripts/reconcile.py:279` `        key = _digest_key(fills, as_of)`
- 可证伪断言：委托目录名是 `2026-10-20`，同一份买入 100 股。第一次不传 `--as-of`，1000 变成 1100，digest 为 `2026-10-20|<指纹>`。第二次加 `--as-of 2026-10-21`，退出码 0，持股变成 1200，新 digest 为 `2026-10-21|<同一指纹>`。第三次再去掉 `--as-of`（键又变回目录名 `2026-10-20`），退出码 0，持股变成 1300。
- 复现方式：`/tmp/r08_probe2`，订单目录 `d2/2026-10-20`，成交 `600000,buy,100,10.00`，起始 `600000,1000`。三次命令只改 `--as-of` 的有无，`--holdings` 每次用上一次的 `--save-holdings`。三次退出码都是 0，持股 1100、1200、1300。
- 建议档：A
- 撤回条件：第二次或第三次退出码 1，持股停在 1100。

### F-38 不传 `--save-holdings` 时不查指纹，退出码 0，并把正确报告改成重复入账的偏离

- 位置：`scripts/reconcile.py:278`
- 该行代码原文：`    if args.save_holdings and not fills.empty:`
- 可证伪断言：同一目录里第一次对账之后 `n_holdings_diff` 是 0、`abs_value_gap` 是 0。用已经入账的持仓再跑同一份成交、并且带上 `--save-holdings`，退出码 1，这两个数不变。去掉 `--save-holdings`、其它参数不变，退出码 0，`n_holdings_diff` 变成 2，`abs_value_gap` 变成 3300.0；`holdings_gap.csv` 写成 000001 实际 100、600000 实际 1200。持仓文件本身不变。
- 复现方式：`/tmp/r08_probe/s0`。起始持仓 `600000,1000` 与 `000001,500`，成交买入 100 @10.00、卖出 200 @11.50，目标 `600000,1100` 与 `000001,300`。第一次退出码 0，json 为 `n_holdings_diff=0`、`abs_value_gap=0.0`，持仓 `000001,300 / 600000,1100`。带 `--save-holdings` 的重跑退出码 1，json 仍是 0。不带 `--save-holdings`、`--holdings` 指向这份已入账持仓，退出码 0，json 变成 2 和 3300.0。
- 建议档：B
- 撤回条件：不带 `--save-holdings` 的这次重跑之后，`holdings_gap.csv` 与 `reconcile.json` 与第一次相同。

### F-39 不传 `--holdings` 时按空仓入账，只有买入的那天会把已有持仓文件盖成这一笔

- 位置：`scripts/reconcile.py:80-81`
- 该行代码原文：
  - `scripts/reconcile.py:80` `    if not path:`
  - `scripts/reconcile.py:81` `        return {}`
- 可证伪断言：已有持仓 `600000,1000` 与 `000001,500`。成交只有 `600000` 买入 100 股，命令带 `--save-holdings` 指向这份文件、不带 `--holdings`。退出码 0，文件变成只有 `600000,100`。标准输出里没有「按空仓对账」。同一命令若成交里还有一笔卖出，空仓超卖走退出码 2，文件保持原样（这条已在 `/tmp/r08_probe/wipe` 看到，不把退出码 2 算进本条的失败）。
- 复现方式：`/tmp/r08_probe2/book.csv` 先写入两只股票。订单目录 `day/`，成交一行买入。命令是 `reconcile.py --orders <day> --fills <fills> --save-holdings <book>`。退出码 0。文件内容从 `600000,1000 / 000001,500` 变成 `600000,100`。路径不存在时才会打印警告，那一行在 `scripts/reconcile.py:84`，本条的调用走不到它。
- 建议档：A
- 撤回条件：上述命令退出码非 0，或者写出的文件仍含 `000001,500` 且 `600000` 不是被收成 100。

### F-40 除权日持有收益为 0，卖出当天现金按原始价入账，10 送 10 的卖出日收益是 -50%

- 位置：`src/a_share_quant/backtest/positions.py:91` 与 `:111`
- 该行代码原文：
  - `src/a_share_quant/backtest/positions.py:91` `        self.cash += shares * price - cost`
  - `src/a_share_quant/backtest/positions.py:111` `        return pos.shares * float(price) * (k / ref)`
- 可证伪断言：零成本、`cash_buffer=0`、价格序列在除权后保持不动。10 送 10（收盘价 10→5，`adj_close` 保持 10）时，除权日权益仍是 1,000,000，收益是 0；下一次把仓卖光的那天权益变成 500,000，日收益 -0.5。现金分红 5%（收盘价 10→9.5，`adj_close` 保持 10）时，除息日权益仍是 1,000,000；卖出日权益变成 950,000，日收益 -0.05。卖出成交价分别是 5.0 与 9.5，股数都是买进时的 100,000，没有随送转增加。
- 复现方式：6 个交易日，日频，`CostModel(0, 0, 0, 0)`，`UniverseRules(min_list_days=0)`，策略前三次信号满仓、第四次空仓。10 送 10 的权益序列是 `1000000, 1000000, 1000000, 1000000, 500000, 500000`，卖出在 2021-01-08，100,000 股 @5.0。分红序列的卖出日收益是 `-0.050000000000000044`。现有 `tests/test_corporate_actions.py` 只断言除权日收益接近 0，卖出日不在那些用例里。
- 建议档：A
- 撤回条件：上述卖出日收益的绝对值小于 1e-9，或者卖出回笼的现金等于卖出前 `position_value`。

### F-41 退市强平的成交记录股数是 0

- 位置：`src/a_share_quant/backtest/engine.py:313`
- 该行代码原文：`                    "shares": pos.shares, "price": px, "cost": 0.0,`
- 可证伪断言：退市日没有行情、强平前持仓 100,000 股时，`note == "forced:delisted"` 的那一行 `shares` 是 0，`price` 是 10.0。权益在强平日保持 1,000,000（现金按强平前的股数入了账）。`state.sell` 在上一行已经把 `pos.shares` 减到 0，记录读的是减完之后的数。
- 复现方式：5 个交易日，只给前两天行情，`delist_date` 为第三天，零成本满仓。`trades` 里强平行是 `shares=0, price=10.0, note=forced:delisted`。`tests/test_lifecycle.py` 的 `test_delisted_holding_is_force_liquidated` 只检查行数、日期和强平后无持仓，不断言 `shares > 0`。
- 建议档：B
- 撤回条件：这行 `shares` 等于强平前的股数（本例 100,000）。

### F-42 退市日当天仍有行情时，强平之后同一天把股票买回来

- 位置：`src/a_share_quant/backtest/engine.py:301` 与 `:310` 与 `:318`
- 该行代码原文：
  - `src/a_share_quant/backtest/engine.py:301` `            # --- 退市清算：已过退市日、当天又无行情的持仓，按最后已知价强平 ---`
  - `src/a_share_quant/backtest/engine.py:310` `                state.sell(sym, pos.shares, px, 0.0)`
  - `src/a_share_quant/backtest/engine.py:318` `            if (pending_weights is not None or pending_sells) and not day_bars.empty:`
- 可证伪断言：`delist_date` 当天 bars 里仍有该股票时，这一天的成交顺序是先记一笔 `forced:delisted`（股数 0），再记一笔买入 100,000 股。当天收盘持仓股数仍是 100,000。下一天再次强平。注释写的「当天又无行情」在 `310` 行之前没有对应判断。`scripts/generate_synthetic_data.py:73` 的上市区间是 `d <= delist_date`，合成数据在退市日保留行情，会走到这条路径。
- 复现方式：5 个交易日每天都有 bar，收盘价恒为 10，`delist_date` 为第三天，策略每天满仓，零成本。成交四行：01-03 买入 100,000；01-04 `forced:delisted` 股数 0；01-04 再买入 100,000；01-05 再一笔 `forced:delisted` 股数 0。`positions` 在 01-03 与 01-04 的股数都是 100,000。
- 建议档：B
- 撤回条件：退市日有行情时，当天收盘持仓为 0，且没有强平之后的新买单。

### F-43 非交易日的 `--as-of` 回滚到相邻月份后，季度月判断跟着变

- 位置：`scripts/live_signal.py:75-76` 与 `:234-235`
- 该行代码原文：
  - `scripts/live_signal.py:75` `        print(f"[warn] {requested} 不是交易日，改用 {earlier[-1].date()}", flush=True)`
  - `scripts/live_signal.py:76` `        return earlier[-1]`
  - `scripts/live_signal.py:234` `    check_only = bool(not args.ignore_quarter and holdings`
  - `scripts/live_signal.py:235` `                      and as_of.month not in DEFAULT_REBALANCE_MONTHS)`
- 可证伪断言：在只剔除周末的日历上，请求日所在月是否属于 `(1, 4, 7, 10)`，与函数实际返回日的月份可以不同。`2025-11-01` 不是季度月，返回 `2025-10-31`，10 月属于季度月，`check_only` 为假，随后会执行 `write_plan`。`2023-04-01` 是季度月，返回 `2023-03-31`，3 月不是季度月，持仓非空且未加 `--ignore-quarter` 时 `check_only` 为真，在写文件前返回。`2024-10-08` 本身是交易日，两个月份判断一致。
- 复现方式：`importlib` 加载 `scripts/live_signal.py`，`TradingCalendar.weekdays("2023-03-01", "2025-11-30")`，调用 `_resolve_as_of`。打印为：`2023-04-01 → 2023-03-31`（季度月变成非季度月），`2023-10-01 → 2023-09-29`（同上），`2025-11-01 → 2025-10-31`（非季度月变成季度月），`2024-10-08 → 2024-10-08`（不变）。没有改 `DEFAULT_REBALANCE_MONTHS`，也没有读数据目录。
- 建议档：B
- 撤回条件：季度判断用的是请求日的月份，或者非交易日直接拒绝而不是改用上一交易日的月份。

### F-44 动量的 `--max-positions` 不进入 `top_n`，名额不够时按代码字典序留下前 N 只

- 位置：`scripts/run_baseline_backtest.py:137` 与 `:164`；截断在 `src/a_share_quant/portfolio/construction.py:29-30`
- 该行代码原文：
  - `scripts/run_baseline_backtest.py:137` `        strategy = TopNMomentum(top_n=pf.get("max_positions", 20),`
  - `scripts/run_baseline_backtest.py:164` `            max_positions=max_pos,`
  - `src/a_share_quant/portfolio/construction.py:29` `                   key=lambda x: (-x[1], x[0]))`
  - `src/a_share_quant/portfolio/construction.py:30` `    items = items[: rules.max_positions]`
- 可证伪断言：`settings.yaml` 的 `max_positions` 是 50，命令行 `--max-positions 10` 时，`max_pos` 是 10，动量 `top_n` 仍是 50。50 只等权经过 `build_target_weights` 后剩下 10 只，代码是 `000000` 到 `000009`，不是动量排序的前 10 名。同文件低波分支用的是 `top_n=max_pos`（`run_baseline_backtest.py:130`）。`397abfb` 的重跑命令没有传 `--max-positions`，所以那次 -14.90% / -16.09% 不走这条截断；传了这个参数的动量回测会走。
- 复现方式：不读行情。令 `args.max_positions=10`、`pf max_positions=50`，按上述两行赋值，得到 `top_n=50`、`PortfolioRules.max_positions=10`。50 个等权代码 `000000`…`000049` 交给 `build_target_weights`，返回 10 个键，首尾是 `000000, 000001, 000002` 与 `000007, 000008, 000009`。
- 建议档：B
- 撤回条件：动量分支的 `top_n` 等于 `max_pos`，或者截断键是动量名次而不是代码字典序。

### F-45 `397abfb` 之后 §12.5 仍写「动量未重跑」，并继续使用 -22.86% / -25.56% / -9.26%

- 位置：`reports/strategy_log.md:541` 与 `:545` 与 `:553`；对照 `reports/strategy_log.md:451` 与 `reports/live_readiness.md:57`
- 该行代码原文：
  - `reports/strategy_log.md:541` `| 12-1 动量 † | -22.86% | -25.56% | -9.26% |`
  - `reports/strategy_log.md:545` `` `run_baseline_backtest.py` 目前没有 reversal 选项，动量未重跑）。 ``
  - `reports/strategy_log.md:553` `三段里唯一稳定的一件事：**动量亏钱**（三段全负，但数值待按新口径重跑确认）。`
  - `reports/strategy_log.md:451` `最终口径重跑（2026-10-21）：2020-2024 **-14.90%**、超额 **-13.44pp**、IR -0.834；`
  - `reports/live_readiness.md:57` `| 12-1 动量 | **-14.90%** / -0.423 / -67.1% | **-16.09%** / -0.628 / -64.1% | 样本不足（见下） |`
- 可证伪断言：同一文件里，§11.1 写最终口径重跑结果为 2020-2024 **-14.90%**、2015-2019 **-16.09%**；§12.5 的表仍是 -22.86% / -25.56% / -9.26%，脚注写「动量未重跑」，下一句又写「前四行已按最终口径重跑」。§12.5 正文写「数值待按新口径重跑确认」。`397abfb` 的 diff 改了 §11 和 `fix_log.md` 第十九节，没有改 §12.5 这几行。`fix_log.md` 第十九节的重跑表没有 Sharpe、没有最大回撤。`-0.423`、`-67.1%`、`-0.628`、`-64.1%` 全仓库只出现在 `live_readiness.md:57`。这四个数是否等于那次重跑的 Sharpe 和最大回撤，**存疑**（没有产物可对，本次按约束没有重跑三段）。
- 复现方式：在当前 HEAD 打开上述行。`git show 397abfb -- reports/strategy_log.md` 的改动停在 §11，§12.5 的动量行不在该 diff 里。
- 建议档：C
- 撤回条件：§12.5 的动量三格与 §11.1 / `fix_log.md` 第十九节是同一套最终口径数字，且脚注不再写「动量未重跑」。Sharpe 与最大回撤那四个数另有重跑产物且逐位一致时，存疑的那半句撤回。

## 结论

有新洞。R07 四档没有在默认同价、同日、带 `--save-holdings` 的重跑上退回去；价格口径、`--as-of`、报告重算、空仓写入这四条仍能让股数或报告偏掉。回测的除权估值只在持有期间成立，卖出和退市日记账是另外两条。`397abfb` 的动量索引没有看出 off-by-one，文档 §12.5 与 §11.1 互相矛盾；那次重跑用的引擎包含 F-40，本次没有重跑三段，不能把 -14.90% 改写成另一个数。

本轮新增 F-36～F-45，共 10 条。其中 F-45 的 Sharpe / 最大回撤半句为存疑。没有撤回已关闭条目。

*本报告只判断代码正确性，不涉及投资建议。回测结果不代表未来收益。*
