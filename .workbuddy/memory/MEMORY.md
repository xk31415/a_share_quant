# 项目长期约定（a_share_quant）

## 文档

- 入口 `HANDOVER.md`（交付状态、上线方案）；`AGENTS.md`（AI 行为约束）；`README.md`（工程说明 +
  已知限制，编号被代码注释引用，新增只追加）；`QUICKSTART.md`（操作手册）；
  `reports/research.md`（研究结论）；`reports/lookahead_audit.md`（防未来函数审计）。
- 2026-10-23 起文档只写当前状态，不写编年史；修复过程写进 commit 消息（问题 → 改动 → 验证，附前后数字）。
  旧的 fix_log、交叉审查轮次（R01~R08）、按日期的记录只在 git 历史里。
- 数字只有一个来源：`results/<EXP>/metrics.json`，当前是 `EXP-20261022-*`。
- 交接文档里的每个数字都要实际数/跑一遍，凭印象的数会错。

## 工作流程

- 改代码后跑 `python -m pytest`（当前 320 passed），失败不许报完成；新测试先红后绿。
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

## 教训

- **测了 A 路径、结论却用来描述 A+B**：写测试时问"这条路径的下一步是什么"（除权之后会卖出）。
- **修复本身是新攻击面**：每加一层保护，先问"这层能不能被反过来用"（换模板、改参数、换编码）。
- **保护机制不能靠"删文件"解除**，必须是显式 `--force`。
- **修"写盘前检查"先枚举所有写盘路径**；输入路径不存在时不能按"空"处理。
- **自己修的东西自己验不准**：按写码时的心智模型去测，绕过路径在模型之外，要做攻击式复现。
- **看到异常小的 n 先停下查分组逻辑**；引用别人算好的差值，列/行/口径逐一对上。

## 环境

- 解释器：`C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 拉数据：`C:\Program Files\Python314\python.exe`（装了 gm 与 pyarrow，需 `GM_TOKEN`）
- PowerShell 用 `;` 连接命令；逗号分隔的代码列表要加引号，否则会变成数组并吃掉前导零。
- 仓库：https://github.com/xk31415/a_share_quant（origin 走 SSH）。
- 行情数据、`results/`、`experiments/`、个人交易记录（`live/*/`、`live/holdings.csv*`、`fills/`）不入库；
  `live/sample.json` 与 `data/MANIFEST.json` 入库。探针目录用 `_dstest/`（已忽略），用完删除。
- `update_dataset.py` / `build_dataset.py` 会改 `data/`，只在主人明确要求时跑。
