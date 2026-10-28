# 仿真盘自动化部署说明

> **目的**：8 周 paper trading，验证「数据 → 信号 → 下单 → 对账」这条执行链路
> 能不能全自动跑稳。**不是验证策略赚钱**——`reports/` 里的结论是「没有策略通过
> 实盘门槛」，仿真账户赚了或亏了都不构成对策略的判断。

## 1. 架构

```
Windows 计划任务（周一~周五）
   │
   ├─ 08:30  live_auto.py --stage data     → update_dataset.py   （gm 数据源）
   │
   ├─ 09:29  live_auto.py --stage trade
   │            ├─ gm_bridge.py pull-holdings   （gm SDK 策略模式）
   │            ├─ live_signal.py               （信号 / pandas）
   │            ├─ （等到 09:30:05 才下单）        order_time
   │            └─ gm_bridge.py place-orders    （gm SDK 策略模式）
   │
   └─ 15:35  live_auto.py --stage settle
                ├─ gm_bridge.py pull-fills      （gm SDK 策略模式）
                └─ reconcile.py                 （对账 / pandas）
```

**只有一个解释器**（3.12，装了 `gm` 与 pandas）。`live_auto.py` 是纯标准库，
负责编排，不 import 任何一个。

**为什么交易要走 gm SDK 的策略模式**：掘金终端的交易接口只对 `gm` 的 `run()`
策略上下文开放，普通脚本直接调 `get_cash()` 会挂死。`gm_bridge.py` 每次操作
都是"写临时策略 → 子进程 run() → 读 JSON → 删临时文件"，子进程隔离掉事件循环。

## 2. 服务器环境（已就绪）

| 项 | 值 |
|---|---|
| 服务器 | 阿里云 ECS `8.134.172.15`，Windows Server |
| 部署目录 | `C:\AppData\a_share_quant` |
| Python（全部） | `...\Python312\python.exe`（gm 3.0.191 / pandas 3.1.0） |
| 默认 `python` | **3.12**（PATH 已调序） |
| 掘金终端 | `emgm3.exe` 运行中，已登录 |
| 仿真账户 | `test`（普通，模拟撮合 7×24），account_id 见 `config/gm_account.yaml` |
| 初始资金 | 100 万（仿真） |

`config/gm_account.yaml` 存 token 与 account_id，**不进 git**。

## 3. 你要做的（一次性）

1. **确认掘金终端已登录**（登录态掉了交易就取不到账户）。
2. **确认仿真资金是 100 万**（掘金终端 → 账户管理 → 出入金）。
3. `auto_trade` 已设为 `true`，计划任务已注册——**周一 08:30 开始自动跑**。
   想先停：`deploy\register_tasks.ps1 -Remove`。

## 4. 验收步骤

```powershell
cd C:\AppData\a_share_quant
$Py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"

# 1) 账户通不通（只读）
& $Py scripts\gm_bridge.py status

# 2) 全链路演练（不写数据、不下单）
& $Py scripts\live_auto.py --stage all --dry-run

# 3) 手动触发一次（实际下单）
Start-ScheduledTask -TaskName a_share_quant_trade

# 4) 看日志
type live\logs\$(Get-Date -Format yyyy-MM-dd).log
```

## 5. 日常

| 想做什么 | 命令 |
|---|---|
| 看今天跑得怎么样 | `type live\logs\<日期>.log` |
| 看账户 | `& $Py scripts\gm_bridge.py status` |
| 手动触发一次 | `Start-ScheduledTask -TaskName a_share_quant_trade` |
| 紧急撤单 | `& $Py scripts\gm_bridge.py cancel-all` |
| 非季度月也强制调仓 | 改 `config/live.yaml` 的 `ignore_quarter: true` |
| 停掉自动化 | `deploy\register_tasks.ps1 -Remove` |

## 6. 关键约定

* **季度调仓**：只在 1/4/7/10 月下单，其余月只检查不下单。
  要改月频，`ignore_quarter: true`。
* **委托口径**：`limit` = 按信号参考价挂单（默认，可能不成交）；
  `market` = 市价（仿真里更接近"努力成交"）。8 周里建议先用 `limit`，
  **不成交本身就是执行链路要暴露的信息**。
* **先卖后买**：下单顺序是先卖腾资金再买，避免买单被"可用不足"卡住。
* **防呆**：单次超过 `max_orders`（默认 200）笔直接拒绝。
* **重复入账**：`reconcile.py` 自带指纹，同一批成交再跑会退出码 1 并拒绝写盘。
* **日志**：`live/logs/<日期>.log`，每个 stage 追加，失败也写在里面。

## 7. 与回测口径的差异（重要）

| 项 | 回测 | 仿真 | 影响 |
|---|---|---|---|
| 成交价 | t+1 开盘 + 滑点模型 | 执行日 09:30 实时价 ×(1±滑点) 挂限价，仿真按限价成交 | 口径已对齐（2026-10-28 起）；仍不可直接比收益 |
| 下单时刻 | t+1 开盘 | 09:30:05（计划任务 09:15 启动、脚本等到此刻） | 09:15 只是竞价申报窗口，无真实价格 |
| 涨跌停判定 | 执行日开盘价 vs 当日涨跌停价 | 执行日 09:30 现价 vs 当日官方涨跌停价 | 口径已对齐，罕见分歧 |
| 跌停能否买入 | 可以 | 可以（已对齐） | 一致 |
| 涨停能否加仓 | 不可以（不看持仓） | 不可以（同） | 一致 |
| 缺涨跌停价 | `blocked:no_limit_data` | 同（含无涨跌幅新股会被拦） | 新股可交易性分歧 |
| 现金不足 | 按比例缩减全部买单 | 同（比例缩减、整手向下取整） | 一致 |
| 整手约束 | 100 股 | 100 股 | 一致 |
| 成本 | 佣金/印花税/滑点模型 | 仿真按规则收 | 大致一致 |

下单时的涨跌停闸门（`gm_bridge.py place-orders`）把结果落两份文件：
`placement.csv`（**实际发送**的委托，对账用它算成交率）与
`placement_skipped.csv`（被拦下的 + 原因）。`orders.csv` 是策略意图，不改动。

**结论用途**：8 周后看的是「链路是否无人值守跑稳、成交率如何、滑点多少」，
不是「策略赚不赚钱」。

## 8. 已知边界

* 掘金终端必须保持登录；登录态掉了 `pull-holdings` 会失败，日志里能看到。
* `pull-fills` 的成交回报字段名以实际返回为准，做了防御式取值；
  第一次跑 settle 后**人工核对** `fills/<日期>.csv` 与终端里的成交是否一致。
* 数据更新依赖 gm 源；`update_dataset` 失败会让 `live_signal` 因数据过期拒绝出信号
  （这是设计，不是 bug）。
