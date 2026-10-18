# 项目长期约定（a_share_quant）

## 语言约定

- **commit 消息一律用中文**。用户明确要求（2026-10-18）。
  不要因为"开源惯例"或"仓库历史是英文"就默认英文——这是个人项目，没有外部协作者，
  中文 log 与中文 README/QUICKSTART/报告保持一致。
- 已推送的历史提交（`c113a19`→`92aa795` 及更早 5 个）保持英文不改，
  不为语言重写历史、不做 force push。

## 工作流程

- 改代码后必须跑 `python -m pytest`（当前 90 passed），失败不许报完成。
- 涉及口径变更（换手率、净值、指标定义）必须同步更新 README 的限制清单
  与 `reports/`，禁止只改代码不改文档——这个项目最忌"文档说 A、代码做 B"。
- 静默失效优先于崩溃处理：缺数据要显式 raise 或 warn，不接受默认放行。
- 修完一批问题写 `reports/fix_log.md`，按"问题 → 改动 → 验证"三段式，附前后数字对比。

## 环境

- 解释器：`C:\Users\67478\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 仓库：https://github.com/xk31415/a_share_quant （origin 走 SSH）
- 沙箱默认拒绝读 `~/.ssh`，`git push` / `ls-remote` 需 `dangerouslyDisableSandbox`；
  被拒后不要重试等价命令，直接让用户自己推。
- `data/interim/`、`results/`、`experiments/` 在 .gitignore 内，合成数据不入库。
