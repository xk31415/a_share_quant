# 项目长期约定（a_share_quant）

## 文档

- 入口 `HANDOVER.md`（交付状态、上线方案）；`AGENTS.md`（AI 行为约束）；`README.md`（工程说明 +
  已知限制，编号被代码注释引用，新增只追加）；`QUICKSTART.md`（操作手册）；
  `reports/research.md`（研究结论）；`reports/lookahead_audit.md`（防未来函数审计）。
- 2026-10-23 起文档只写当前状态，不写编年史；修复过程写进 commit 消息（问题 → 改动 → 验证，附前后数字）。
  旧的 fix_log、交叉审查轮次（R01~R08）、按日期的记录只在 git 历史里。
- 数字只有一个来源：`results/<EXP>/metrics.json`，当前是 `EXP-20261022-*`。
- 交接文档里的每个数字都要实际数/跑一遍，凭印象的数会错。
- 接手 AI / 新会话先看 `HANDOVER.md` §0「快速上手」（一屏速览）；审查类任务另给
  开工指令 + checklist 的独立文档，修完即删、不入库。

## 工作流程

- 改代码后跑 `python -m pytest`（当前 389 passed），失败不许报完成；新测试先红后绿。
- 真实下单**非幂等**：禁止 `run_step_retry` 包裹（部分成功重试 = 重复买入）；只有幂等步骤
  （补数据/拉持仓/拉成交/演练下单）可重试。
- 改口径后 grep 全文找旧数字；收尾动作是从文件第一行读到最后一行（表头、摘要、清单都要看）。
- 给默认值加参数化时，默认分支输出必须逐位不变，用旧结果回归一次。
- 缺数据显式 raise 或 warn，不默认放行。
- commit 消息一律中文；不 force push、不 `--no-verify`。

## 领域口径（踩过的坑）

- A 股卖出也受整手约束，零股只在清仓时一次性卖出（上证函〔2014〕301 号、深证会〔2014〕72 号）。
- 涨跌停不用 `pre_close × 10%` 推算（ST、创业板、新股、2026-07-06 主板 ST 新规各不相同）。
- 指数代码会与个股代码撞车（`000905`、`000852`），基准必须从 `benchmark.csv` 读。
- 等权策略的及格线是全宇宙月频等权（对齐 5 月起窗口 +9.85% / -6.61% / +17.09%），不是指数；
  参照值必须标调仓频率与窗口，算对齐窗口时不能先切片再暖机。
- gm 的 `sec_name` 是当前简称，不能判历史 ST；`is_st` 只取 `get_symbols(trade_date)` 截面。
- 评策略先看 `effective_days / trading_days`，低于约 70% 的段不与其他段并列。
- 因子结论必须过三关：流动性分档内、行业归因后、样本外。
- 涨跌停判定分两层，别合成一处：**信号层**只做「已持有的跌停冻结」（规划现金账，t 收盘近似、方向保守）；
  **下单层**做执行日闸门（`gm_bridge.apply_execution_gate`，官方 upper/lower_limit + 现价，对齐 execution.py）。
  用信号日收盘判涨停会把「专买涨停股」的策略 100% 自我否决（2026-10-27 踩过）。
- 数据更新要 `GM_TOKEN` 环境变量（gm 数据接口只认它），由 live_auto 从 `config/gm_account.yaml` 注入；
  拉行情与交易是两套凭据路径，改一边不代表另一边通。

## 教训

- **测了 A 路径、结论却用来描述 A+B**：写测试时问"这条路径的下一步是什么"（除权之后会卖出）。
- **修复本身是新攻击面**：每加一层保护，先问"这层能不能被反过来用"（换模板、改参数、换编码）。
- **保护机制不能靠"删文件"解除**，必须是显式 `--force`。
- **修"写盘前检查"先枚举所有写盘路径**；输入路径不存在时不能按"空"处理。
- **自己修的东西自己验不准**：按写码时的心智模型去测，绕过路径在模型之外，要做攻击式复现。
- **看到异常小的 n 先停下查分组逻辑**；引用别人算好的差值，列/行/口径逐一对上。
- **生成代码（字符串拼出来的子进程模块）是独立命名空间**，父进程函数名一律不可见：
  要么内联要么显式 import；空数据路径测不出这种错（空集合不进循环体），必须用"有数据"探针。

## 环境

- 解释器：`C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 拉数据：`C:\Program Files\Python314\python.exe`（装了 gm 与 pyarrow，需 `GM_TOKEN`）
- PowerShell 用 `;` 连接命令；逗号分隔的代码列表要加引号，否则会变成数组并吃掉前导零。
- 仓库：https://github.com/xk31415/a_share_quant（origin 走 SSH）。
- **GitHub 被墙时怎么推**（2026-10-27 踩过，`github.com` 被污染成 28.0.0.8、22/443 全断）：
  本机 `127.0.0.1:7897` 是 Clash 混合端口，但**只有 socks5h 协议到 GitHub 通、http 代理不通**，
  且节点只放行 **ssh.github.com:443**（GitHub 官方备用端口，22 被节点屏蔽）。用
  `C:\Users\67478\AppData\Local\Temp\socks5_ssh.py`（SOCKS5→stdio 隧道）配：
  `GIT_SSH_COMMAND="ssh -o ProxyCommand=<py> <脚本> %h %p"` +
  `git -c "url.ssh://git@ssh.github.com:443/xk31415/a_share_quant.git.insteadOf=git@github.com:xk31415/a_share_quant.git" push origin <branch>`。
  写这类隧道脚本时 socket 只有 `sendall`、BufferedReader 只有 `read1`，混用会静默失败。
- **只在 `D:\a_share_quant` 上改代码**，它 checkout 的是 `main`。C 盘的 worktree
  `Worktrees/a_share_quant/main-bfb8497b`（分支 `workbuddy/main-bfb8497b`）2026-10-27 起弃用，
  不要在那里提交——它会落后 main，且 `reports/review/` 里有遗留的未跟踪文件。
- 行情数据、`results/`、`experiments/`、个人交易记录（`live/*/`、`live/holdings.csv*`、`fills/`）不入库；
  `live/sample.json` 与 `data/MANIFEST.json` 入库。探针目录用 `_dstest/`（已忽略），用完删除。
- `update_dataset.py` / `build_dataset.py` 会改 `data/`，只在主人明确要求时跑。
- 仿真服务器：`8.134.172.15`，项目在 `C:\AppData\a_share_quant`，Python 3.12 单解释器
  （3.10/gmtrade 已卸载）；计划任务 `/tr` 必须写**绝对脚本路径**——schtasks 没有工作目录
  参数，相对路径在默认 cwd=System32 下直接退出码 2（2026-10-26/27 踩过）。
  服务器是**精简副本**：没有 `tests/`、没装 pytest，验证靠 import 冒烟 + dry-run。
- **服务器 SSH 认证**（2026-10-28 起）：管理员组走
  `C:\ProgramData\ssh\administrators_authorized_keys`（Windows 特殊路径，ACL 必须是
  Administrators+SYSTEM 完全控制，否则 sshd 忽略整个文件）。**标准运维 key：
  `~/.nightwatch/ops_ed25519`**（指纹 `SHA256:3WShXTvNpbx6INscfeh3wsJC2FyaROcplWqNWkzyaiU`，
  别名 nightwatch-ops），用法：
  ```
  ssh -i C:/Users/67478/.nightwatch/ops_ed25519 -o BatchMode=yes Administrator@8.134.172.15 "<cmd>"
  ```
  远端默认 shell 是 **cmd.exe**，所以命令用 cmd 语法（`cd /d`、`set X=1&&`、`&&`）。
  **只授权 1 把**（nightwatch-ops）。`~/.ssh/id_ed25519`（GitHub 通用那把）2026-10-28
  曾追加作兜底，当天已撤销——一把 GitHub key 反过来能登生产服务器，泄露面没必要放大。
  `PasswordAuthentication` 仍是默认 yes，密码还能兜底；真关掉前先确认每台机器的公钥都进了文件。
  **路径坑**：Git Bash 会把命令里的 `C:\x\y` 转成 `C:/x/y`。Python 吃正斜杠所以没事，
  但 `findstr` / `icacls` 这类把 `/` 当开关的会炸（报「无法打开 C:xxx」）——要么用
  `%ProgramData%\...` 变量，要么这类文件操作用 paramiko/SFTP 做。
  paramiko 连法：`connect(..., key_filename=~/.nightwatch/ops_ed25519, look_for_keys=False)`。
- **代理软件会干扰到服务器的 SSH**：2026-10-27 晚 `Connection reset by peer`（ping 通、22 开、
  握手完成后在认证阶段被切），退掉代理立即恢复。同类现象先怀疑代理，别急着查 sshd。
- NightWatch 看门狗：源码 `D:\StudyDemo\csharp\NightWatch`，服务器 `C:\AppData\NightWatch`
  （Windows 服务 `NightWatch`，HttpHeartbeat:5200，企业微信+钉钉双通道）。
  2026-10-27 时 `FileHeartbeats` 是空的——实盘没被监控，这是"出问题没告警"的根因。
