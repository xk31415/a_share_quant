# R07 Grok 审查（验收轮：攻击 R06 修复）

- 角色：Grok
- 轮次：R07
- reviewed_commit: `a855a37b9b8b2a51fe8accaf4d8ca358dc8307e6`
- 观察到的 HEAD：`605b42e`（`git log --oneline -5`：`605b42e` docs: 补记 R07 任务书与题型更换理由 / `5225a14` review: R07 任务书 / `da4f228` docs: 补记 R06 四档修复 / `a855a37` R06 修复 / `236359f` docs: 补记 R06 判定要点）。`a855a37` 在历史里，`a855a37..605b42e` 没有代码改动，下面引用的行在当前 HEAD 仍在。
- pytest：`python -m pytest` → **247 passed in 3.58s**。
- 环境：Windows（XINGKONG-PC），`D:\a_share_quant`，CPython 3.13.14（MSC v.1944 64 bit），pandas 3.0.5、numpy 2.5.3、pyarrow 25.0.1。所有探针都放在 `D:\a_share_quant\_dstest\`，结束时已经 `Remove-Item -Recurse -Force`。没有写 `live/`，没有跑 build/update_dataset，也没有跑完整回测。开始和结束时的 `git status --short` 都是空的。
- **§4 结论：能但要带条件。** 条件：① 修 F-32（指纹只取规范化后的核心四列，并按 (symbol, side) 聚合），或者在流程里写死"同一天的成交只用同一种券商导出模板对账"；② 洞 ① 改成日期+指纹双键，或者每日流程明写"不能回溯跑旧成交"；③ 修 F-33（空批次不查指纹），或者写明"退出码 1 时先核对、不要直接删 digest"；④ 修 F-34，在那之前退出码 1 时 `live/<日期>/` 下的报告文件不可信；⑤ 每周至少一次用券商持仓导出核对 `--save-holdings` 的输出（股数逐只相等）。这一条是指纹之外唯一能兜住洞 ①②③ 的检查。

## 总评

F-26 的指纹保护能吸收的扰动都实测过了：字段两侧加空格、`10.0`/`10.00`、`100.0` 股、`证券买入/证券卖出` 这类方向写法、行顺序对调。这几种重跑都被拦下，退出码 1，没有写盘。原地更新（`--holdings` 和 `--save-holdings` 指向同一个文件）也被正确拦下，文件没变。同日拆两批（内容不同）正常入账，没有误伤。

洞 ③ 找到了：指纹吃的是**规范化后 DataFrame 的列布局和 dtype**，不是成交内容本身。同一批成交换一种导出格式就认不出来，股数被加第二遍，退出码 0（F-32）。指纹又只看内容、不看日期，内容恰好相同的合法批次会被误拦。最常见的情况是连续两次空成交（F-33）。另外，被拦下的重跑（退出码 1）仍然会把按重复入账算出的持仓偏离写进 `holdings_gap.csv` 和 `reconcile.json`，覆盖第一次正确运行的报告（F-34）。还有一处小问题：`unmatched_fills.csv` 会残留（F-35）。

F-30 的副作用：非季度月多跑的部分（`build_plan` 加渲染）实测约 4–5 ms，整个进程约 1.7 s。除 `write_plan` 外没有第二处写盘，前后对全仓库 1189 个文件的长度和 mtime 做了快照，比对零差异。空仓建仓不受 check_only 影响。`--dry-run` 和 check_only 叠加时 check_only 先返回，不写文件。§3 没有新发现。

## 探针方法

`_dstest/probe.py` 用 `subprocess.run([python, "scripts/reconcile.py", "--orders", ..., "--fills", ..., "--holdings", ..., "--save-holdings", ..., "--out", "_dstest/out/<tag>"])` 逐条执行，打印命令、输出的最后几行、退出码，以及 `--save-holdings` 文件的内容。指纹用 `scripts/reconcile.py` 自己的 `_fills_digest(normalize_fills(pd.read_csv(path, dtype={"symbol": str})))` 计算，取前 12 位。

公共数据：

```
_dstest/orders/orders.csv : symbol,side,shares,price / 600000,buy,100,10.00 / 000001,sell,200,11.50
_dstest/h0.csv            : symbol,shares / 600000,1000 / 000001,500
_dstest/A.csv             : symbol,side,shares,price,fee / 600000,buy,100,10.00,5.00 / 000001,sell,200,11.50,5.00
```

基线 S0（保护本身是好的）：

```
$ python scripts/reconcile.py --orders _dstest/orders --fills _dstest/A.csv --holdings _dstest/h0.csv --save-holdings _dstest/h1.csv --out _dstest/out/s0a
   成交指纹 7383eeb64015 已记入 h1.csv.digest（防重复入账）
   exit=0  _dstest/h1.csv = symbol,shares | 000001,300 | 600000,1100
$ python scripts/reconcile.py --orders _dstest/orders --fills _dstest/A.csv --holdings _dstest/h1.csv --save-holdings _dstest/h2.csv --out _dstest/out/s0b
   这批成交已经入过账（指纹 7383eeb64015，见 h1.csv.digest）。
   exit=1  _dstest/h2.csv = <不存在>
```

### 被吸收的扰动（没有洞，列出供复核）

下面每条都是拿 `h1.csv` 重跑同一批成交的另一种写法：

| 探针 | 成交 CSV 写法 | 指纹 | 结果 |
|---|---|---|---|
| P1 空格 | ` 600000 , buy , 100 , 10.00 , 5.00` | 7383eeb64015 | exit=1，h2 不存在 |
| P2a | `10.0,5.0` / `11.5,5` | 7383eeb64015 | exit=1，h2 不存在 |
| P2c | 股数写成 `100.0` / `200.0` | 7383eeb64015 | exit=1，h2 不存在 |
| P6b | `证券买入` / `证券卖出` | 7383eeb64015 | exit=1，h2 不存在 |
| P6c | 两行对调 | 7383eeb64015 | exit=1，h2 不存在 |
| P7 原地 | `--holdings _dstest/hin.csv --save-holdings _dstest/hin.csv` 跑两次 | 7383eeb64015 | 第 1 次 exit=0 → `000001,300 / 600000,1100`；第 2 次 exit=1，文件不变 |
| P8 同日拆批 | A1=只买 600000，A2=只卖 000001，h0→s1→s2 | f179eb0141ad / ed9873aa1461 | 两次都是 exit=0，s2 = `000001,300 / 600000,1100`，与 h1 相同，**没有误伤** |

P2a 里 `11.5,5` 这一行的 fee 写成整数 `5`，但同一列另一行是 `5.0`，整列被解析成 float，所以指纹不变。整列全是整数时结果不同，见 F-32。

## 发现

### F-32 指纹绑定列布局和 dtype：同一批成交换一种导出格式就认不出来，股数被加第二遍（洞 ③）

- reviewed_commit: `a855a37`
- 位置：`scripts/reconcile.py:92-94`；成因还涉及 `src/a_share_quant/live/reconcile.py:52`、`:63-66`
- 该行代码原文：
  - `scripts/reconcile.py:92` `    cols = list(fills.columns)`
  - `scripts/reconcile.py:93` `    ordered = fills[cols].sort_values(by=cols, kind="mergesort").reset_index(drop=True)`
  - `scripts/reconcile.py:94` `    h = pd.util.hash_pandas_object(ordered, index=False)`
  - `src/a_share_quant/live/reconcile.py:52` `    out = fills.copy()`（多出来的列原样保留，原列顺序也保留）
  - `src/a_share_quant/live/reconcile.py:66` `        out["fee"] = float("nan")`（没有 fee 列时补 NaN，和有 fee 的同批成交指纹不同）
- 可证伪断言：同一批成交（股票、方向、股数、成交价都相同），只要改动下面任意一项，`_fills_digest` 就不同。拿已经吸收过这批成交的持仓重跑，退出码 0，买入股数再加一次，卖出股数再减一次：
  - (a) 列顺序；
  - (b) 多一列无关字段；
  - (c) 有没有 fee 列；
  - (d) 价格列全为整数时写成 `10` 还是 `10.00`（int64 和 float64 的哈希不同）；
  - (e) fee 列全为整数时写成 `5` 还是 `5.00`；
  - (f) 明细（同一只股票两笔 60+40）还是汇总（一笔 100）。
- 复现方式（均为 `--holdings _dstest/h1.csv`；h1 已经吸收过 A，A 的指纹是 7383eeb64015）：

```
=== P3_reorder  指纹 352d9147810b  原始内容: ['side,symbol,price,shares,fee', 'buy,600000,10.00,100,5.00', 'sell,000001,11.50,200,5.00']
$ python scripts/reconcile.py --orders _dstest/orders --fills _dstest\P3_reorder.csv --holdings _dstest/h1.csv --save-holdings _dstest/h2_P3_reorder.csv --out _dstest/out/P3_reorder
   新持仓（2 只）已写入 _dstest\h2_P3_reorder.csv
   exit=0  _dstest/h2_P3_reorder.csv = symbol,shares | 000001,100 | 600000,1200

=== P4_extracol  指纹 29ae473a63d8  原始内容: ['symbol,side,shares,price,fee,trade_id', '600000,buy,100,10.00,5.00,T001', '000001,sell,200,11.50,5.00,T002']
   exit=0  _dstest/h2_P4_extracol.csv = symbol,shares | 000001,100 | 600000,1200

=== P5_nofee  指纹 913f97eefd1b  原始内容: ['symbol,side,shares,price', '600000,buy,100,10.00', '000001,sell,200,11.50']
   exit=0  _dstest/h2_P5_nofee.csv = symbol,shares | 000001,100 | 600000,1200

=== P2d_fee_int  指纹 695482b7bcf2  原始内容: ['symbol,side,shares,price,fee', '600000,buy,100,10.00,5', '000001,sell,200,11.50,5']
   exit=0  _dstest/h2_P2d_fee_int.csv = symbol,shares | 000001,100 | 600000,1200

=== P6_detail  指纹 f9320cc7a3ab  原始内容: ['symbol,side,shares,price,fee', '600000,buy,60,10.00,3.00', '600000,buy,40,10.00,2.00', '000001,sell,200,11.50,5.00']
   exit=0  _dstest/h2_P6_detail.csv = symbol,shares | 000001,100 | 600000,1200
```

  (d) 整数价格单独做了一组。委托单 `_dstest/orders2` 的价格是 10.00 / 12.00：

```
指纹 4523b6076242 35e701a55161
$ python scripts/reconcile.py --orders _dstest/orders2 --fills _dstest\B_dec.csv --holdings _dstest/h0.csv --save-holdings _dstest/hB1.csv --out _dstest/out/B1
   exit=0  _dstest/hB1.csv = symbol,shares | 000001,300 | 600000,1100
$ python scripts/reconcile.py --orders _dstest/orders2 --fills _dstest\B_int.csv --holdings _dstest/hB1.csv --save-holdings _dstest/hB2.csv --out _dstest/out/B2
   exit=0  _dstest/hB2.csv = symbol,shares | 000001,100 | 600000,1200
```

  `B_dec.csv` 的行是 `600000,buy,100,10.00,5.00` 和 `000001,sell,200,12.00,5.00`；`B_int.csv` 的行是 `600000,buy,100,10,5.00` 和 `000001,sell,200,12,5.00`。

- 失败场景：当天先用客户端的"当日成交"导出对账（常见情况：没有 fee 列，或是明细粒度）。次日拿到"交割单"（有 fee，多出成交编号、发生日期等列，列顺序不同，可能是汇总粒度），按日常流程再对一遍，或者重试时换了导出文件，保护全部失效。
  - **buy 和 sell 的不对称**：买入重复永远不会触发 `critical`。卖出重复只有在超过持仓时才被 `超过持仓` 拦下，退出码 2（见下方 P11）；在持仓以内的卖出重复会静默把股数少记一遍（P3：000001 300→100）。
  - 后果的方向：买入多记，下一次 `live_signal` 会认为持仓偏多，于是少买或者下达卖不出去的卖单。卖出多记，持仓被低估，下一次会多买，引发现金不足告警。

```
=== P11 卖出清仓的重复：h0 里 000001 500 股全卖，再用换列顺序的同批成交重跑
$ python scripts/reconcile.py --orders _dstest/orders4 --fills _dstest\clear.csv --holdings _dstest/h0.csv --save-holdings _dstest/c1.csv --out _dstest/out/P11a
   exit=0  _dstest/c1.csv = symbol,shares | 600000,1000
$ python scripts/reconcile.py --orders _dstest/orders4 --fills _dstest\clear_re.csv --holdings _dstest/c1.csv --save-holdings _dstest/c2.csv --out _dstest/out/P11b
     000001 卖出 500 股超过持仓 0 股
   [warn] 存在必须人工处理的问题，未写入 _dstest/c2.csv。先核对成交，再决定是否入账
   exit=2  _dstest/c2.csv = <不存在>
```

- 建议档：A（进 paper trading 前修，或者用流程条件替代）。改法附注：指纹只取 `aggregate_fills(fills)[["symbol","side","shares","price"]]`，`shares` 转 int，`price` 转 float 并 `round(3)`，列顺序固定，不包含 fee 和其他列。这样 (a)–(f) 都能吸收。
- 撤回条件：上面任一组在 a855a37 上重跑得到 exit=1，或者第二次的持仓不变。

### F-33 指纹只看内容不看日期：内容相同的合法批次被误拦，连续两次空成交必被拦

- reviewed_commit: `a855a37`
- 位置：`scripts/reconcile.py:90-91`、`:238-240`
- 该行代码原文：
  - `:90` `    if fills.empty:`
  - `:91` `        return "empty"`
  - `:239` `            if (prev is not None and prev.exists()`
  - `:240` `                    and prev.read_text(encoding="utf-8").strip() == dg):`
- 可证伪断言：
  - (a) 上一份持仓是由空成交批次写出的（digest=`empty`）时，下一批空成交必定 exit=1，并且拒绝写出新持仓。全是 0 股的回报行（撤单）被 `normalize_fills` 过滤成空表，也算空成交。空批次本来就没有可重复入账的股数，这次拦截纯属误伤。
  - (b) 两批合法成交的内容恰好相同时，第二批 exit=1，没有入账。例如同一委托分两次回报，各 100 股 @10.00、佣金各 5 元。
- 复现方式：

```
=== P10 连续两天全部未成交（成交表只有表头）
$ python scripts/reconcile.py --orders _dstest/orders --fills _dstest\empty.csv --holdings _dstest/h0.csv --save-holdings _dstest/e1.csv --out _dstest/out/P10a
   成交指纹 empty 已记入 e1.csv.digest（防重复入账）
   exit=0  _dstest/e1.csv = symbol,shares | 600000,1000 | 000001,500
$ python scripts/reconcile.py --orders _dstest/orders --fills _dstest\empty.csv --holdings _dstest/e1.csv --save-holdings _dstest/e2.csv --out _dstest/out/P10b
   这批成交已经入过账（指纹 empty，见 e1.csv.digest）。
   再跑一遍会把股数加第二遍，因此拒绝写盘。
   确认要重跑就先删掉 _dstest\e1.csv.digest。
   exit=1  _dstest/e2.csv = <不存在>
--- P10' 成交表里只有 0 股行（撤单回报），同样被归一成空
$ python scripts/reconcile.py --orders _dstest/orders --fills _dstest\zero.csv --holdings _dstest/e1.csv --save-holdings _dstest/e3.csv --out _dstest/out/P10c
   这批成交已经入过账（指纹 empty，见 e1.csv.digest）。
   exit=1  _dstest/e3.csv = <不存在>

=== P9 同日两批内容恰好相同（委托 200 股，分两次各成交 100 股 @10.00、各 5 元佣金）
$ python scripts/reconcile.py --orders _dstest/orders3 --fills _dstest\half.csv --holdings _dstest/h0.csv --save-holdings _dstest/p1.csv --out _dstest/out/P9a
   600000 buy 委托 200 成交 100（缺 100）
   exit=0  _dstest/p1.csv = symbol,shares | 000001,500 | 600000,1100
$ python scripts/reconcile.py --orders _dstest/orders3 --fills _dstest\half.csv --holdings _dstest/p1.csv --save-holdings _dstest/p2.csv --out _dstest/out/P9b
   这批成交已经入过账（指纹 f179eb0141ad，见 p1.csv.digest）。
   exit=1  _dstest/p2.csv = <不存在>
```

  其中 `empty.csv` = `symbol,side,shares,price,fee`（只有表头），`zero.csv` 只有一行 `600000,buy,0,10.00,0`，`half.csv` 只有一行 `600000,buy,100,10.00,5.00`。

- 失败场景：
  - 任务书 §2 说"误伤比漏拦更糟"。场景 (a)：调仓日挂单因涨停、停牌全部没成交，次日继续挂、继续没成交，第二天对账就退出码 1，提示"先删掉 digest"。用户照做以后，这份持仓的保护就没了。下一次真重跑时，洞 ② 等于被系统引导出来。链式脚本（`e1 → e2`）里 e2 不存在，下一步读不到文件。
  - 场景 (b)：在券商按"逐笔回报"拆成小批、每批单独对账时会出现，频率低于 (a)。
  - 不拦的话不会多记股数，因为两批都是真实成交；拦了反而会少记 100 股。
- 建议档：B。改法附注：`fills.empty` 时跳过指纹检查，并且不覆盖 digest；非空批次用"日期 + 指纹"或券商成交编号做键，这一步和洞 ① 的双键修法一起做。
- 撤回条件：上述命令在 a855a37 上第二次运行得到 exit=0 并写出 e2/p2。

### F-34 被拦下的重跑（退出码 1）仍然把按重复入账算出的报告写进输出目录，覆盖第一次的正确报告

- reviewed_commit: `a855a37`
- 位置：`scripts/reconcile.py:214-222`（写报告）早于 `:238-244`（指纹拦截）
- 该行代码原文：
  - `:214` `    matched.to_csv(out_dir / "orders_vs_fills.csv", index=False, encoding="utf-8")`
  - `:216` `        gap.to_csv(out_dir / "holdings_gap.csv", index=False, encoding="utf-8")`
  - `:220` `    (out_dir / "reconcile.json").write_text(`
  - `:241` `                raise SystemExit(`
- 可证伪断言：在默认 `--out`（即委托单所在目录，实盘中是 `live/<日期>/`）下，第一次正确对账以后，用同一批成交重跑会被指纹拦下（exit=1），但 `holdings_gap.csv` 和 `reconcile.json` 已经被这次重跑改写：`actual_shares` 是重复入账后的数，`n_holdings_diff` 从 0 变成 2，`abs_value_gap` 从 0 变成 3300。`--save-holdings` 之外，这条写盘路径既绕过了 `critical`，也绕过了指纹。
- 复现方式（`_dstest/day/` 内放 `orders.csv` 和 `target.csv`，`target.csv` = `600000,1100,10.00 / 000001,300,11.50`，不传 `--out`）：

```
$ python scripts/reconcile.py --orders _dstest/day --fills _dstest/A.csv --holdings _dstest/h0.csv --save-holdings _dstest/d1.csv
   exit=0  _dstest/d1.csv = symbol,shares | 000001,300 | 600000,1100
   [第 1 次后] holdings_gap.csv = symbol,target_shares,actual_shares,shares_gap,price,value_gap,material | 000001,300,300,0,11.5,0.0,False | 600000,1100,1100,0,10.0,0.0,False
   [第 1 次后] reconcile.json abs_value_gap/n_holdings_diff = {'n_holdings_diff': 0, 'abs_value_gap': 0.0}
$ python scripts/reconcile.py --orders _dstest/day --fills _dstest/A.csv --holdings _dstest/d1.csv --save-holdings _dstest/d2.csv
   这批成交已经入过账（指纹 7383eeb64015，见 d1.csv.digest）。
   exit=1  _dstest/d2.csv = <不存在>
   [被拦重跑后] holdings_gap.csv = symbol,target_shares,actual_shares,shares_gap,price,value_gap,material | 000001,300,100,-200,11.5,-2300.0,True | 600000,1100,1200,100,10.0,1000.0,True
   [被拦重跑后] reconcile.json abs_value_gap/n_holdings_diff = {'n_holdings_diff': 2, 'abs_value_gap': 3300.0}
```

- 失败场景：持仓文件被保护住了，但 `live/<日期>/` 下给人看的"结果偏差"报告被改成一份并不存在的偏离（模块说明把这一项列为"最重要"）。第一次运行的正确报告被覆盖，无法恢复。操作者事后翻报告，会看到每只都"偏离一手"。critical（退出码 2）时也同样先写报告，这一点作为诊断材料可以接受，本条只主张退出码 1 这一路。
- 建议档：B。改法附注：把指纹检查前移到第 214 行之前；或者退出码 1 时只打印，不写 `out_dir`。
- 撤回条件：上述第二次运行以后，`holdings_gap.csv` 和 `reconcile.json` 与第一次运行后的内容一致。

### F-35 `unmatched_fills.csv` 只在有无对应委托的成交时写、从不清理，修正后重跑会残留旧文件

- reviewed_commit: `a855a37`
- 位置：`scripts/reconcile.py:217-219`
- 该行代码原文：
  - `:217` `    if unmatched is not None and len(unmatched):`
  - `:218` `        unmatched.to_csv(out_dir / "unmatched_fills.csv", index=False,`
- 可证伪断言：同一个输出目录里，先跑一份含无对应委托成交的成交表（exit=2），再跑修正后的成交表（exit=0）。之后 `reconcile.json` 的 `n_unmatched_fills` 是 0，但 `unmatched_fills.csv` 仍然列着上次那笔。
- 复现方式（`_dstest/day2/orders.csv` = `600000,buy,100,10.00`；`bad.csv` 比 `good.csv` 多一行 `601318,buy,100,50.00,5.00`）：

```
$ python scripts/reconcile.py --orders _dstest/day2 --fills _dstest\bad.csv --holdings _dstest/h0.csv --save-holdings _dstest/g1.csv
   [warn] 存在必须人工处理的问题，未写入 _dstest/g1.csv。先核对成交，再决定是否入账
   exit=2  _dstest/g1.csv = <不存在>
   ls day2: ['orders.csv', 'orders_vs_fills.csv', 'reconcile.json', 'unmatched_fills.csv']
$ python scripts/reconcile.py --orders _dstest/day2 --fills _dstest\good.csv --holdings _dstest/h0.csv --save-holdings _dstest/g1.csv
   exit=0  _dstest/g1.csv = symbol,shares | 000001,500 | 600000,1100
   ls day2: ['orders.csv', 'orders_vs_fills.csv', 'reconcile.json', 'unmatched_fills.csv']
   reconcile.json n_unmatched_fills = 0  | unmatched_fills.csv = symbol,side,shares,price,fee | 601318,buy,100,50.0,5.0
```

- 失败场景：只影响给人看的报告，不改持仓。目录里的两个文件互相矛盾，有人会照残留文件去追查一笔已经排除的"手动单"。`holdings_gap.csv` 在 `target.csv` 不存在时同样不写、不清，原理相同。
- 建议档：C。改法附注：没有 unmatched 时删掉旧文件，或者每次都写一份空表。
- 撤回条件：第二次运行以后 `unmatched_fills.csv` 不存在，或者为空表。

## §3 F-30 副作用（无新发现，证据如下）

运行方式：`_dstest/ls_probe.py` 用 `importlib` 加载 `scripts/live_signal.py`，然后调用 `main()`。当天（2026-10-21）属于 10 月，是季度月，而数据新鲜度熔断（15 天）不允许把 `--as-of` 设到 9 月，所以 CLI 走不到 check_only。探针在进程内把 `DEFAULT_REBALANCE_MONTHS` 改成 `(1, 4, 7)`，让 2026-10-20 变成"非季度月"，只为走到这个分支。没有改任何文件。

另外两处进程内包装：`build_plan` 和 `LocalFileProvider.get_bars` 包了计时；`write_plan` 包了一层记录调用，记录完照常调用原函数。

固定参数：`--data-dir data/processed_2025_2026 --position 0.40 --cash 500000 --out-dir D:\a_share_quant\_dstest\lsout`。持仓文件是 `_dstest/hold_ls.csv`，取 sample 前 5 只，各 1000 股。`--sample` 用默认的 `live/sample.json`，只读。环境变量 `PYTHONDONTWRITEBYTECODE=1`。

| 运行 | 条件 | 关键输出 | exit | 耗时 | write_plan 调用 |
|---|---|---|---|---|---|
| R1 | 非季度 + 有持仓 | `2026-10-20 不是季度调仓月 (1, 4, 7)，本月只检查不下单`；`订单 47 笔`；`非季度调仓月：以上只是**检查**，未写委托文件。` | 0 | total=1.01s，get_bars=0.34s，build_plan=0.004s | `[]` |
| R2 | R1 + `--dry-run` | 只打印 check_only 那句，**不打印** `--dry-run：未写文件。` | 0 | total=0.93s，build_plan=0.004s | `[]` |
| R3 | 真实季度月（不打补丁）+ 有持仓 + `--dry-run` | `--dry-run：未写文件。` | 0 | total=0.95s，build_plan=0.005s | `[]` |
| R4 | 非季度 + 空持仓文件（只有表头）+ `--dry-run` | 没有 check_only 提示；`订单 18 笔`；`--dry-run：未写文件。` | 0 | total=0.89s | `[]` |
| R5 | 非季度 + 空持仓文件，不加 `--dry-run` | `已写入 D:\a_share_quant\_dstest\lsout\2026-10-20`，内含 orders.csv、summary.json、target.csv | 0 | total=0.89s | `['D:\\a_share_quant\\_dstest\\lsout']` |
| R6 | 非季度 + 不传 `--holdings` + `--dry-run` | 没有 check_only 提示；`--dry-run：未写文件。` | 0 | total=0.93s | `[]` |
| R7 | 非季度 + 有持仓 + `--ignore-quarter --dry-run` | `--dry-run：未写文件。` | 0 | total=0.94s | `[]` |
| R8 | R1 同参数，`Measure-Command` 测整个进程 | — | — | wall=1.73s | — |

- **耗时**：旧代码的 `return` 位于 `get_bars` 和取价之后（diff 中原第 229–236 行），所以读行情（约 0.3 s）旧路径本来就在执行。新增执行的只有 `build_plan`（4–5 ms）、成本估算和 `render_text`，没有从秒级变成分钟级。
- **额外写盘**：读代码，`src/a_share_quant/live/`、`data/loader.py`、`universe/` 里只有 `signal.py:104-105`（`Sample.save`，只在 `--create-sample` 分支调用）和 `signal.py:442-448`（`write_plan`）会写盘。`loader.py` 的 `_cache` 是进程内的 dict。
  - 实测：R1–R7 前后对全仓库（排除 `.git`、`_dstest`）1189 个文件做"路径 | 长度 | mtime"快照，`Compare-Object` 零差异。
  - `live/` 里原有的 `sample.json` 和 `2026-10-20/` 三个文件没有变动。
- **空仓建仓**：`check_only = bool(not args.ignore_quarter and holdings and ...)`（`scripts/live_signal.py:234-235`）中的 `holdings` 是 `{}`，为假。R4、R5、R6 证实空持仓或不传持仓时不进入 check_only，R5 正常写出了计划。
- **`--dry-run` 叠加**：`if check_only: ... return`（`:293-296`）在 `if args.dry_run:`（`:297`）之前，两者都不写文件，只是提示语不同（R2）。没有任何组合会在 check_only 下写盘。
- 行为变化，不算缺陷，只作记录：非季度月现在会执行第 241–245 行的 `unpriced` 检查和 `build_plan` 内的估值 `ValueError`。旧代码会在这之前以 0 退出，现在持仓缺价时会非零退出。另外 check_only 下即使出现"已不可投资、清仓"之类的告警，退出码仍然是 0，靠定时任务的退出码看不到这些告警。这一点没有可证伪的错误主张，**存疑**，不编号。

## §4 结项判断

**能但要带条件。**

就代码正确性而言，没有 blocking 级问题。F-32 和 F-33 都发生在"同一批成交被再次对账"这个操作上，而不是正常的一天一批链式流程。持仓文件的偏差可以用券商持仓导出发现和修正，不会悄悄污染回测或信号逻辑本身。条件：

1. **F-32（洞 ③）**：进 paper trading 前，把指纹改成"按 (symbol, side) 聚合后的 symbol / side / int 股数 / round(价格, 3)"，不包含 fee 和其他列。或者在每日流程里写死：同一天的成交只用同一种券商导出模板，并且只对账一次。
2. **洞 ①**（任务书已知）：改成日期+指纹双键。或者在每日流程里明写"不能回溯跑旧成交；要重做某天，从那天之前的持仓文件重新链式往后跑"。
3. **F-33**：空批次不查、不写指纹。在修之前，流程里写明"退出码 1 时先核对是不是空成交或内容相同的分批，不要直接删 digest"。
4. **F-34**：在修之前，退出码 1 时 `live/<日期>/holdings_gap.csv` 和 `reconcile.json` 不可信，应以第一次运行的结果为准，或者重跑一次正确的命令重新生成。
5. **对账兜底**：8 周内至少每周一次，把 `--save-holdings` 的输出和券商"持仓查询"导出逐只比对股数。指纹只防"同一批"，这一条才能兜住洞 ①②③。
6. 每次调用 `reconcile.py` 都要检查退出码：0 是正常；1 是重复，没有写持仓；2 是人工处理，没有写持仓。链式脚本遇到非 0 必须停下。

## 清理与状态

- `D:\a_share_quant\_dstest\` 已经 `Remove-Item -Recurse -Force`，`Test-Path` 返回 False。
- 结束时 `git status --short` 为空，与开始时相同。`git fetch` 后 `git status -sb` 显示 `## main...origin/main`，本地 HEAD 与 origin/main 都是 `605b42e2fb96ca581002af879ebcf530f31d9e75`。
- 附带说明：前半段的指纹计算用 `importlib` 加载了 `scripts/reconcile.py`，刷新了已经被 `.gitignore` 忽略的 `scripts/__pycache__/reconcile.cpython-313.pyc`（pytest 也会刷新它）。之后的运行都设置了 `PYTHONDONTWRITEBYTECODE=1`。

*本报告只判断代码正确性，不涉及投资建议。回测结果不代表未来收益。*
