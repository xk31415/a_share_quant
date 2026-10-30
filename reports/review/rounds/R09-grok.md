# R09 Grok：上线前最终门禁

> 角色：Grok。轮次文件名沿用已删除协议里的 `R<nn>-grok.md`（`git show 1504d36:reports/review/PROTOCOL.md`）。
> 本文件只记录这次门禁。不恢复 `LEDGER.md`，不重开已关闭的 F 编号。

## 元信息

- 角色：Grok
- 轮次：R09（最终门禁；观察对象是 paper trading 的文档/代码是否同口径，不给买卖建议）
- reviewed_commit：`65a3a29d8bbc0772674abe4ac6243c5eb6502830`
- 与 HEAD 是否一致：是（审查时 `main` 已快进到该 SHA；主题「文档重整：只保留当前状态，删除过程性记录，准备交付」）
- 本轮范围：`4abc596`（PR #8 交接文档合入）之后到 HEAD 的文档与实盘入口；pytest；密钥；股票池；数据指纹。未跑 `build_dataset.py` / `update_dataset.py`，未写 `live/`，未重跑长回测。
- 环境：云端 Python 3.12.3、pandas 3.1.0、numpy 2.5.4、pyarrow 26.0.0、pytest 9.1.1。README 写的本机是 Python 3.13。本轮不能代替主人机器上的逐位复现。

---

## 结论

**真钱：NO-GO。**

**paper trading：GO-WITH-CONDITIONS。**

「可以上线赚钱」这条说法，和仓库自己的交付结论对不上。`HANDOVER.md` §1 写的是：交付的是一套可以进入 paper trading 的系统；paper 尚未开始；真钱不建议，8 周 paper 达标后最多 20% 资金试跑 3 个月、不改参数。`reports/research.md` §4.3 写的是没有任何选股或择时策略通过实盘门槛。README 限制第 23 条同样写明滑点、成交率、成本都还是配置值。

paper 的 QUICKSTART 路径（`live_signal.py` → 券商或人工成交 → `reconcile.py`）在代码里存在，退出码与 `AGENTS.md` / `QUICKSTART.md` §5.5 一致，全量测试 296 passed。没有一条已复现的缺陷表明「按 QUICKSTART 抄命令会把持仓写错」。所以 paper 可以按文档开始，但下面几条不满足就不能开始：

1. 本云端没有 `bars.parquet`。`data_manifest.py verify` 三份都跳过，仍退出码 0（见 R09-3）。开始 paper 的机器上必须看到 2020-2024 与 2015-2019 的 `[ ok ]`，不能只看退出码。
2. 每日命令必须显式带 `--position 0.40`。脚本默认是 1.0（R09-2）。
3. 对账以 `QUICKSTART.md` §5.5 为准。`reconcile.py --help` 里 `--as-of` 那一行和同页正文相反（R09-1）。
4. `update_dataset.py` 仍是红线：只在主人明确要求、且本机有掘金 `gm` 与 `GM_TOKEN` 时跑。本轮没有跑。
5. 仓库里没有 paper 撮合器。HANDOVER §2.2 的「次日开盘价、成交股数 = 委托股数、且不超过次日成交量的 10%」要由模拟盘或人工 fills 实现，再交给 `reconcile.py`。

**阻断项**

| 门 | 判定 | 阻断 |
|---|---|---|
| 真钱 | NO-GO | 文档自己阻断：策略未过门槛；paper 的五条工程判据还没有记录；滑点未实测 |
| paper | GO-WITH-CONDITIONS | 无「按手册命令会写错账」的已复现阻断。无行情、默认满仓、`--as-of` 说明写反，这三条会使「现在就开 paper」失败或下错规模 |

---

## 核对清单

### 1. HEAD 与 `4abc596` 之后的提交

`git rev-parse HEAD` = `65a3a29d8bbc0772674abe4ac6243c5eb6502830`（2026-10-23 08:04 +0800，作者 hyg）。

审查开始时云端快照停在 `9eef49f`，`git fetch origin main` 后快进两笔：`1504d36`、`65a3a29`。`4abc596..HEAD` 共 16 笔：

| SHA | 日期 | 主题 |
|---|---|---|
| `53c2ef1` | 2026-10-22 | 补 PR #8 漏的一处 +13.2，并标注两份历史报告时效 |
| `f483756` | 2026-10-22 | 补记 PR #8 修正与 lookahead 未完成项 |
| `5ab98e8` | 2026-10-22 | 沉淀「入口未改」的教训 |
| `26e496f` | 2026-10-22 | 真实数据防未来函数重审；6.2pp 差异破案 |
| `18f615f` | 2026-10-22 | 实盘停牌/跌停冻结、动量容量、基准缺日配对 |
| `2e318f4` | 2026-10-22 | 实盘跌停可新开仓；status 落后告警 |
| `1b163c1` | 2026-10-22 | 基准缺日落盘、越界豁免改显式开关 |
| `ac065d6` | 2026-10-23 | 涨跌停两侧回归；status 整窗缺失也告警 |
| `269fb2b` | 2026-10-23 | 补挂卖单按代码排序，去掉 `PYTHONHASHSEED` 漂移 |
| `7d27f57` | 2026-10-23 | `benchmark_extra_days` 只数策略区间内多出日 |
| `ec821c6` | 2026-10-23 | 整仓回归文档；限制第 39–41 条；测试 281→286 |
| `b2f3c0f` | 2026-10-23 | 撤回用 `sec_name` 判 ST（当前简称，属未来函数） |
| `844d263` | 2026-10-23 | 已有卖单的票不再从 `pending_sells` 重复补挂 |
| `9eef49f` | 2026-10-23 | 上述文档收口；测试数 287 |
| `1504d36` | 2026-10-23 | 实盘演练修复：持仓路径、资金不足、退出码、券商中文列 |
| `65a3a29` | 2026-10-23 | 删过程文档，只留当前状态 |

`65a3a29` 删除了 `reports/review/`（含 LEDGER、PROTOCOL、R01–R08）、`reports/live_readiness.md`、`reports/strategy_log.md`、`reports/fix_log.md`。HANDOVER §5 与 AGENTS §0 写明这些只留在 git 历史。本文件是删除之后新加的门禁记录。

### 2. pytest

命令：`python -m pytest`。结果：**296 passed in 9.84s**（退出码 0）。与 HANDOVER §2.1、AGENTS §1、README §1 写的 296 一致。测试不读真实行情（README §3）。

### 3. HANDOVER / AGENTS / LEDGER 表头

HEAD 上没有 `reports/review/LEDGER.md`。最后一版在 `1504d36`（`git show 1504d36:reports/review/LEDGER.md`）：

- 表头是「**R08 已结项**（十条已修，数字已重发；审查不再开轮）」，下一步是 paper trading，并写明 6.2pp 已破案（宇宙等权误记 +4.95%，真实 +9.85%）。
- 表头不是「R05 仍在进行」。
- 正文有一节标题为「R05 当时的指令（已过时，不是当前下一步）」，里面仍印着旧年化 `+9.81% / -13.49% / +5.28%` 和旧超额 `+8.37 / -1.10 / -4.94`。该节自己标了过时，且已随 `65a3a29` 离开工作区。

现行主表在 `HANDOVER.md` §4、`README.md` §10、`reports/research.md` §3，三者同为：

| 策略 | 2020-2024 | 2015-2019 | 2025-2026 |
|---|---|---|---|
| 等权 50 只 | +11.15% | -10.33% | +6.11% |
| 宇宙等权（5 月起） | +9.85% | -6.61% | +17.09% |

当前 `*.md` / `*.py` / `*.yaml` / `*.json` 检索不到 `+13.2` 或 `13.2pp`。`+8.37/-1.10/-4.94` 只出现在 `AGENTS.md` §1，作为「改完表格、正文还留旧超额」的历史例子，没有被写成当前 alpha。

仍把旧数字叫成「主口径」的一处见 R09-4（`9.81%`）。

6.2pp：HANDOVER §7 第 4 条把它写成已经发生过的错账（先切片再暖机，宇宙等权从 +9.85% 错成 +4.95%）。现行研究门槛用的是 +9.85% / -6.61% / +17.09%（`reports/research.md` §2、§4.3）。它不是还开着的 alpha 缺口。

### 4. 实盘就绪口径与仍开放的风险

`reports/live_readiness.md` 与 `reports/strategy_log.md` 已在 `65a3a29` 删除。删除前的 `live_readiness.md`（`1504d36`）约束是持仓 40–80 只、月频、单票 2.5%。现行工作区没有再引用这组数。现行方案以 `reports/research.md` §4.1 / §8 与 `HANDOVER.md` §2 为准：冻结样本 100 只、季度（1/4/7/10 月）、`--position 0.40`、不选股不择时。HANDOVER、README §11、QUICKSTART §5、`scripts/live_signal.py` 模块说明这四点一致。

`config/settings.yaml` 的 `max_positions: 50` 与 `benchmark: "000300"` 是回测默认。`live/sample.json` 的 `n` 是 100；`live_signal.py` 在样本文件与 `--n` 不一致时以样本为准。不带 `--benchmark` 的基线回测会落到沪深 300，对不上 QUICKSTART 里中证 1000 的那条锁（`annualized_return = 0.0610828468971103`）。

paper 的下一步与红线，三份现行文档一致：

- 下一步是 8 周 paper（HANDOVER §8 第 1 条），判据在成交率 / 滑点 / 持仓偏离 / 费用偏差 / 数据校验，不在 8 周收益。
- `update_dataset.py` 与 `build_dataset.py` 不是日常命令（AGENTS §5、HANDOVER §8）。paper 每日流程又确实要跑 `update_dataset.py`（QUICKSTART §5.3）。这是「主人点头之后才能跑」的操作条件，不是把红线删了。

本环境**不能**复核「三段结果在不同 pandas/pyarrow 上 float64 逐位相同」：`results/` 只有 `.gitkeep`，没有 `results/EXP-20261022-*`。已入库的 `reports/lowvol_defensive.json` 里，2025-2026 等权 `ew.ann` 是 `0.061082846897110965`，与文档字符串 `0.0610828468971103` 不是同一个 float64（差约 6.7e-16）。百分数都是 +6.11%。该 json 的 Sharpe 是年化/波动（2025-2026 等权 0.312），`research.md` §3 的 Sharpe 0.401 来自未入库的 `metrics.json`。两套数不能在本环境对质；展示用的年化百分数对得上 json 的 `ann`（2020-2024 等权 0.11155、低波 0.14289）。

`data/MANIFEST.json` 的摘要与 `research.md` §1 的规模一致：2020-2024 为 4711 只 / 5,114,194 行 / 1212 日；2015-2019 为 3715 / 3,652,321 / 1219；2025-2026 为 4620 / 1,974,465 / 433，末日 `2026-10-20`。这是指纹文件里的摘要，不是本机重新数出来的行数。

仍写在 README 限制清单、且和 paper 有关的开放项（不重开旧 F 编号）：

- 第 4 条：退市整理期没有时点信号。2025-2026 的 60 日反转因此含 3 只误入股；冻结样本等权方案不靠这条反转收益。
- 第 6 条：2015-2019 有 2 只孤儿代码，现有数据集未重建。paper 用的是 `processed_2025_2026`。
- 第 7 条：代码无交易所后缀。`live/sample.json` 的 1000 只候补里有 `000905`（名次 248）、`000852`（名次 436）；前 100 只没有这两只。`excluded_symbols` 只列了 `000300`。无行情表时分不清它们是个股还是指数代码。
- 第 17 条：成交量上限用全日量，开盘可成交量更小。paper 的 10% 规则没有单独脚本。
- 第 23 条：没有策略过实盘门槛。
- 第 26 条：实盘用 t 日收盘判涨跌停，回测用 t+1 开盘。
- 第 27 条：非季度月「只检查」即使有告警，退出码仍是 0（资金不足拒单除外）。
- 第 29 条：100 只、40% 仓位，文档写总资产约 80 万才通过。`301589` 在冻结样本第 14 名，与 HANDOVER §3「一手超过 2 倍预算」的例子是同一只代码。本轮没有重跑那次资金扫描。

删除前的 LEDGER（`1504d36`）状态汇总是待判定 0、R08 已结项。其后的 `1504d36` 又修了一批实盘演练问题，HANDOVER §3 写成已修复，对应测试在 `tests/test_reconcile.py` 与 `tests/test_live_signal.py`，本轮 pytest 通过。没有一条仍标着开放、并且会单独挡住 paper 的旧台账条目留在现行文档里。

### 5. 关键脚本与退出码

存在且与 AGENTS §5 的命令同形：

- `scripts/live_signal.py`：`--data-dir`、`--cash`、`--holdings`、`--position`、`--out-dir`、`--sample` 都在。
- `scripts/reconcile.py`：`--orders` 可以是目录或 `orders.csv`；`--fills`、`--holdings`、`--save-holdings`、`--from-empty`、`--force` 都在。
- 没有 `scripts/paper_*.py`。paper 与实盘共用上述两个入口。AGENTS 写明演练要用临时 `--sample` / `--out-dir`，不要碰 `live/`。主人的每日流程按 QUICKSTART 写到 `live/` 与 `fills/`，这两处已被 `.gitignore` 忽略；`live/sample.json` 是入库的冻结样本。

`reconcile.py` 退出码（模块说明、`QUICKSTART.md` §5.5、AGENTS §5 三处一致；实现见 `scripts/reconcile.py` 末段）：

| 码 | 含义 | 写盘 |
|---|---|---|
| 0 | 正常 | 报告 + 新持仓 + 指纹 |
| 1 | 只表示这批成交已入过账 | 什么都不写 |
| 2 | 超卖 / 超量 / 对不上单，或输入错误（缺文件、缺列、列名歧义、`--holdings` 路径不存在、已有持仓却没传 `--holdings` 也没 `--from-empty`） | 业务问题只写报告；`SystemExit("说明")` 的输入错误什么都不写 |

`live_signal.py`：非 0（`SystemExit` 字符串，退出码 1）= 没写委托。非季度月检查完 `return`，退出码 0，与限制第 27 条一致。

AGENTS 的对账示例是已有持仓的路径。首次空仓要加 `--from-empty`，写在 QUICKSTART §5.5，不在 AGENTS 那一条里。

### 6. 数据

云端只有 `data/MANIFEST.json` 和四个 `.gitkeep`。没有 `data/processed`、`data/processed_2015_2019`、`data/processed_2025_2026` 的 parquet。

`python scripts/data_manifest.py verify` 实测：

```text
[skip] processed: ... 在本机不存在（未重建数据，跳过）
[skip] processed_2015_2019: ...
[skip] processed_2025_2026: ...
校验通过（3 份本机缺失，已跳过）
```

退出码 0。因此本轮**只对代码和文档做门禁**，没有复核 HANDOVER §3 的 100 万建仓演练，也没有复核基线 `0.0610828468971103`。

### 7. 密钥与配置

- 跟踪文件里没有 `.env`。只有 `.env.example`，`GM_TOKEN=` 是空的，并写明真实密钥不要入库。
- `git ls-files` 没有 pem、私钥或 credentials 文件。
- `config/settings.yaml` 与 `config/universe.yaml` 没有 token。`GM_TOKEN` 只在代码里从环境变量读（`scripts/build_dataset.py`）。
- `.gitignore` 忽略 `.env`、`data/processed*/*`、`results/*`、`live/*/`、`live/holdings.csv*`、`fills/`，并保留 `live/sample.json` 与 `data/MANIFEST.json`。

### 8. 会挡住 paper 的旧审查条目

见清单第 4 节：最后一版 LEDGER 的待判定是 0。现行文档把剩余风险放在 README 第 12 节，没有一条写成「paper 禁止开始」。真钱被第 23 条与 HANDOVER §1 挡住。

### 9. 科创板 / 北交所

`config/universe.yaml` 的 `allowed_prefixes` 只有沪深主板与创业板（`600/601/603/605/000/001/002/003/300/301`）。注释写明排除 `688/689`、北交所 `43/83/87/92`、B 股 `200/900`。`src/a_share_quant/universe/builder.py` 按前缀剔除，不在白名单里的代码进不了股票池。

`live/sample.json`（`created` / `as_of` = 2026-10-20，`n` = 100，`ranked` 1000 只，`data_dir` = `processed_2025_2026`）：1000 只里没有 `688/689/43/83/87/92/200/900` 前缀。前 100 只的前缀计数为 600×24、300×22、002×21、000×11、603×8、301×5、601×3、605×2、003×2、001×2。

---

## 发现问题

### R09-1 `reconcile.py --help` 的 `--as-of` 与同页正文相反

- 主张：`python scripts/reconcile.py --help` 的选项说明仍写「`--as-of` 参与重复入账判定，跨天同内容的成交不算重复」。同一份帮助正文和实现都是：日期不进指纹，改 `--as-of` 不能把同一批成交再入一次账。
- 证据：`scripts/reconcile.py` 选项帮助原文：

```221:223:scripts/reconcile.py
    ap.add_argument("--as-of", default=None,
                    help="对账日期（默认取委托单所在目录名）。参与重复入账判定，"
                         "跨天同内容的成交不算重复")
```

同文件帮助正文写的是「**日期不参与判定**，`--as-of` 只写进历史供人看」。`_digest_history` 原文：「每行格式 `YYYY-MM-DD <指纹>`，日期只为人看，**判定只比对指纹**。」（`scripts/reconcile.py` 约第 152 行）。`_infer_as_of` 原文：「这个日期**只写进历史供人看，不参与重复判定**」（约第 177–178 行）。

- 复现：本机执行 `python scripts/reconcile.py --help`。正文含「日期不参与判定」；`options` 段的 `--as-of` 含「参与重复入账判定，跨天同内容的成交不算重复」。代码侧由 `tests/test_reconcile.py` 的 `test_changing_as_of_does_not_bypass_the_duplicate_guard` 锁定；本轮 `python -m pytest` 为 296 passed。
- 失败场景：操作者只读参数说明，认为换一个 `--as-of` 就应该入账。实际得到退出码 1。若因此加上 `--force`，同一批股数会再记一遍。正确命令（QUICKSTART，不靠改日期绕过）不会触发。
- 严重度：中
- 撤回条件：`--as-of` 的 help 改成与 `_digest_history` 相同的句子（日期只写入 `.digest` 供人看，判定只比较股数指纹），或有测试锁住这句帮助文本。

### R09-2 不传 `--position` 时按满仓出单

- 主张：交付方案的目标仓位是 0.40；`live_signal.py` 在参数省略时使用 1.0。
- 证据：

```202:203:scripts/live_signal.py
    ap.add_argument("--position", type=float, default=1.0,
                    help="目标仓位 w（0~1）。回撤≈w×满仓回撤；保守建议 0.4~0.5")
```

`HANDOVER.md` §1 与 QUICKSTART §5.1 的方案是 `--position 0.40`。`reports/research.md` §8.2 把可接受回撤约 -25% 对应到仓位 0.39。帮助文本写「保守建议 0.4~0.5」，默认值仍是 1.0。三份手册里的示例命令都写了 `--position 0.40`。

- 复现：读上述默认值。`python scripts/live_signal.py --help` 的 `--position` 说明没有打印默认 1.0。本环境无行情，没有真正生成委托。
- 失败场景：首次建仓漏写 `--position`，委托按满仓计算。这和书面的 40% 仓位、以及「按可承受回撤定仓位」不是同一张单。抄 QUICKSTART 全文则不会发生。
- 严重度：中
- 撤回条件：默认改为 0.40，或未在命令行显式传入 `--position` 时拒绝写委托。

### R09-3 三份数据都不在时，指纹校验仍退出码 0 并打印「校验通过」

- 主张：`bars.parquet` 全部缺失时，`python scripts/data_manifest.py verify` 退出码为 0，末行是「校验通过（3 份本机缺失，已跳过）」。
- 证据：

```155:179:scripts/data_manifest.py
        if not (data_dir / "bars.parquet").exists():
            print(f"[skip] {key}: {data_dir} 在本机不存在（未重建数据，跳过）")
            skipped += 1
            continue
        ...
    if failed:
        print(f"校验失败：{failed} 份不一致" + (f"，{skipped} 份本机缺失" if skipped else ""))
        return 1
    print("校验通过" + (f"（{skipped} 份本机缺失，已跳过）" if skipped else ""))
    return 0
```

- 复现：2026-10-23 云端 checkout 执行该命令。三行 `[skip]`，末行「校验通过（3 份本机缺失，已跳过）」，进程退出码 0。HANDOVER §2.1 与 QUICKSTART 第 1 节要求看到 `[ ok ]`（代码打印的是 `[ ok ]`，中间有空格）。
- 失败场景：只把退出码当门禁的脚本会认为指纹已核对。随后 `live_signal.py` 会因数据目录不存在而退出，所以这条本身不会直接写出委托。危险是把这次「校验通过」记成 HANDOVER 前置检查已完成。
- 严重度：低
- 撤回条件：全部目标缺失时退出码非 0，或成功句不再使用「校验通过」。若保留「无数据的环境必须退出码 0」这一设计，则前置检查的书面步骤继续要求看到 `[ ok ]`——HANDOVER 已经这样写，本条只在「只看退出码」时成立。

### R09-4 低波检查脚本仍把 9.81% 写成现行主口径

- 主张：`scripts/lowvol_defensive_check.py` 的 `load_eq` 注释把 `metrics.summarize` 的主口径写成 9.81%，并说该表与 `reports/research.md` §3 用同一条规则。现行 §3 的 2020-2024 等权是 +11.15%。9.81% 是除权卖出结算重跑之前的数字。
- 证据：

```46:49:scripts/lowvol_defensive_check.py
    不截的话这张表会用"全区间"口径：2020-2024 的等权是 9.15%，
    而主口径（`metrics.summarize`）是 9.81%——同一策略两个数，跨节对比
    就会减出假的差额（R05 F-23）。这里直接调主口径的 `trim_warmup`，
    保证这张表和 `reports/research.md` §3用同一条规则。
```

同文件第 35–39 行把实验目录标成 `EXP-20261022-*`（注释称是除权卖出修复之后的一批）。`reports/research.md` §3 表内是 `+11.15%`。删除前的台账（`git show 1504d36:reports/review/LEDGER.md`）写过 `9.81% / -13.49% / 5.28%` 必须在那次修复后重发；重发后的工作区数字是 +11.15% / -10.33% / +6.11%。

- 复现：对照上述注释与 `reports/research.md` §3 第一行策略数字。本环境没有 `D:/a_share_quant/results`（脚本把根目录写死为 `Path(r"D:/a_share_quant")`），没有重跑该脚本。主张只覆盖这句注释，不覆盖 `trim_warmup` 本身。
- 失败场景：读者把 9.81% 当成当前等权年化，与 HANDOVER §4 的 +11.15% 同时使用。
- 严重度：低
- 撤回条件：注释改成现行 §3 的数字，或明确写成「重跑前的例子，不是当前 §3」。

---

## 建议下一步

1. 真钱保持关闭。8 周 paper 的五条工程指标（HANDOVER §2.2）还没有记录之前，不加资金、不改 `live/sample.json`、不改 `config/` 与 `--position`。
2. 在有三份 parquet 的机器上做 HANDOVER §2.1：pytest 296、`data_manifest.py verify` 对 2020-2024 与 2015-2019 打出 `[ ok ]`、`git diff --quiet live/sample.json`。基线年化那条锁本环境无法复核；有数据时再对 `results/EXP-20261022-*` 的 `metrics.json`，审查轮不重跑长回测。
3. 修 R09-1 的帮助文本。paper 第一次对账之前，操作者按 QUICKSTART §5.5 理解指纹：日期不能用来区分两批相同股数；同日两批真成交才用 `--force`；不要删 `.digest`。
4. 每日 `live_signal.py` 显式写 `--position 0.40` 与 `--cash`。漏参数时的默认满仓是 R09-2。
5. `update_dataset.py` 仍只在主人明确要求时、用装了掘金 gm 的解释器跑。本轮未跑，也不应在审查机上跑。
6. paper 成交需要模拟盘导出或按次日开盘手工构造 fills。仓库不生成这张表。每周用券商持仓对 `live/holdings.csv`（README 限制第 28 条）。

---

*本轮是研究流程的门禁记录，不构成投资建议。回测数字未在本环境重跑。*
