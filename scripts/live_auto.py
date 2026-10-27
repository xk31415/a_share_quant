"""实盘/仿真日循环主控：数据 -> 持仓 -> 信号 -> 下单 -> 对账。

设计
----
* **纯标准库**，不 import pandas / gmtrade —— 这样它在 3.10 / 3.12 都能跑，
  不会因为"包装在另一个解释器里"而挂。具体工作全部 `subprocess` 交给
  对应解释器：信号/对账用 3.12（pandas），交易用 3.10（gmtrade）。
* **幂等**：每个 stage 可以重复跑。持仓导出每次覆盖、委托有 `--max-orders`
  防呆、对账有指纹防重复入账（reconcile 自带）。
* **失败要响**：任何一步非零退出都写 `live/logs/<date>.log` 并以非零退出，
  计划任务里配邮件告警。**不静默吞错**。

三个 stage
----------
    data    （建议 08:30）更新行情数据到 T-1
    trade   （建议 09:15）导出持仓 -> 算信号 -> 下单
    settle  （建议 15:35）取成交回报 -> 对账 -> 更新持仓

用法
----
    python scripts/live_auto.py --stage data
    python scripts/live_auto.py --stage trade            # 实际下单
    python scripts/live_auto.py --stage trade --dry-run  # 只算不下
    python scripts/live_auto.py --stage settle
    python scripts/live_auto.py --stage all --dry-run    # 全链路演练

配置见 `config/live.yaml`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Windows 控制台是 GBK，子进程输出里可能有替换符 \ufffd，print 会炸。
# 统一降级成可打印字符，**不让编码问题毁掉一次自动化运行**。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _safe(s: str) -> str:
    return (s or "").encode("gbk", errors="replace").decode("gbk", errors="replace")


# ---------- 配置 ----------
def load_yaml(p: Path) -> dict:
    """最小 YAML 读取（只支持本项目 config 的扁平/两层结构），避免依赖 PyYAML。"""
    if not p.exists():
        return {}
    out: dict = {}
    stack: list[tuple[int, dict]] = [(-1, out)]
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip().strip("'\"")
        # 去掉行内注释：`200  # 超过这个笔数` -> `200`
        if "#" in v:
            v = v.split("#", 1)[0].strip().strip("'\"")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else out
        if v == "":
            child: dict = {}
            parent[k] = child
            stack.append((indent, child))
        else:
            # 类型猜测：数字 / 布尔 / 字符串
            if v.lower() in ("true", "false"):
                parent[k] = v.lower() == "true"
            else:
                try:
                    parent[k] = int(v)
                except ValueError:
                    try:
                        parent[k] = float(v)
                    except ValueError:
                        parent[k] = v
    return out


def config() -> dict:
    c = load_yaml(ROOT / "config" / "live.yaml")
    c.setdefault("python_data", r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe")
    c.setdefault("python_trade", r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe")
    c.setdefault("data_dir", "data/processed_2025_2026")
    c.setdefault("holdings", "live/holdings.csv")
    c.setdefault("fills_dir", "fills")
    c.setdefault("out_dir", "live")
    c.setdefault("sample", "live/sample.json")
    c.setdefault("position", 1.0)
    c.setdefault("auto_trade", False)
    c.setdefault("order_type", "limit")
    c.setdefault("max_orders", 200)
    c.setdefault("ignore_quarter", False)
    c.setdefault("log_dir", "live/logs")
    return c


# ---------- 基础设施 ----------
def log_dir(cfg: dict) -> Path:
    d = ROOT / cfg["log_dir"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_step(name: str, cmd: list[str], log, timeout: int = 1800,
             allow_fail: bool = False) -> int:
    """跑一步，输出同时进控制台与日志。返回退出码。"""
    printable = " ".join(cmd)
    log.write(f"\n{'='*70}\n[{dt.datetime.now():%H:%M:%S}] {name}\n$ {printable}\n")
    log.flush()
    print(f"\n--- {name} ---", flush=True)
    # 子进程统一输出 UTF-8：Windows 控制台默认 GBK，中文会被编码毁掉
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), timeout=timeout,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
    except subprocess.TimeoutExpired:
        log.write(f"[FAIL] {name} 超时（>{timeout}s）\n")
        print(f"[FAIL] {name} 超时", flush=True)
        return 124
    except FileNotFoundError as e:
        log.write(f"[FAIL] 找不到可执行文件：{e}\n")
        print(f"[FAIL] 找不到可执行文件：{e}", flush=True)
        return 127

    for stream, tag in ((p.stdout, "out"), (p.stderr, "err")):
        if stream:
            log.write(stream)
            if not stream.endswith("\n"):
                log.write("\n")
    log.write(f"[exit {p.returncode}] {name}\n")
    log.flush()

    # 控制台只给摘要，避免计划任务邮件被刷屏
    tail = (p.stdout or "").strip().splitlines()[-12:] if p.stdout else []
    for ln in tail:
        print(_safe(ln), flush=True)
    if p.returncode != 0:
        err_tail = (p.stderr or "").strip().splitlines()[-6:]
        for ln in err_tail:
            print("  ! " + _safe(ln), flush=True)
        if not allow_fail:
            print(f"[FAIL] {name} 退出码 {p.returncode}", flush=True)
    return p.returncode


def run_step_retry(name: str, cmd: list[str], log, max_retries: int = 3,
                   wait_seconds: int = 60, timeout: int = 1800,
                   allow_fail: bool = False) -> int:
    """带重试的 run_step。掘金终端每天自动重启 1~2 次，连接会掉。

    对 gm 相关步骤（拉持仓/下单/取成交/更新数据）自动重试。
    gm_bridge 内部已有连接层重试，这里是**进程级**兜底（终端在脚本执行中间挂掉）。
    """
    import time
    rc = run_step(name, cmd, log, timeout=timeout, allow_fail=True)
    for attempt in range(2, max_retries + 1):
        if rc == 0 or (allow_fail and rc == 1):
            break
        print(f"  {name} 失败（rc={rc}），等 {wait_seconds}s 重试 "
              f"（第 {attempt}/{max_retries} 次）", flush=True)
        log.write(f"  [retry] {name} rc={rc}, attempt {attempt}\n")
        time.sleep(wait_seconds)
        rc = run_step(name, cmd, log, timeout=timeout, allow_fail=True)
    if rc != 0 and not allow_fail:
        print(f"[FAIL] {name} 最终退出码 {rc}（重试 {max_retries} 次后）", flush=True)
    return rc


def today() -> str:
    return dt.date.today().isoformat()


def find_recent_orders(cfg: dict, max_age_hours: float = 24.0) -> Path | None:
    """找最近写入的 orders.csv。

    `live_signal.py` 按**数据日**（as_of）落盘到 `live/<as_of>/orders.csv`，
    而自动化按**执行日**运行，两者常常不是同一天（周五数据、周一执行）。
    所以这里按修改时间找最近 24 小时内的那份，而不是硬拼日期。
    """
    import time
    root = ROOT / cfg["out_dir"]
    if not root.exists():
        return None
    candidates = sorted(root.glob("*/orders.csv"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    now = time.time()
    for p in candidates:
        if now - p.stat().st_mtime <= max_age_hours * 3600:
            return p
    return None


# ---------- 各 stage ----------
def stage_data(cfg: dict, log, args) -> int:
    """更新行情数据。用 3.12（gm 数据源）。"""
    cmd = [cfg["python_data"], "scripts/update_dataset.py",
           "--data-dir", cfg["data_dir"], "--source", "gm"]
    if args.dry_run:
        cmd.append("--dry-run")
    rc = run_step_retry("update_dataset", cmd, log, timeout=1800)
    return rc


def stage_trade(cfg: dict, log, args) -> int:
    """导出持仓 -> 算信号 -> 下单。"""
    py_t, py_d = cfg["python_trade"], cfg["python_data"]
    date = today()
    holdings = ROOT / cfg["holdings"]
    cash_file = ROOT / cfg["log_dir"] / f"cash_{date}.txt"

    # 1) 从掘金取持仓与现金
    rc = run_step_retry("pull-holdings", [
        py_t, "scripts/gm_bridge.py", "pull-holdings",
        "--out", cfg["holdings"],
        "--cash-out", str(cash_file),
    ], log, timeout=120)
    if rc != 0:
        return rc

    cash = None
    if cash_file.exists():
        try:
            cash = float(cash_file.read_text(encoding="utf-8").strip())
        except ValueError:
            cash = None

    # 2) 算信号。--as-of 传【今天】，让季度判断用执行日的月份（R08 F-43）；
    #    数据不够新时 live_signal 自己会回滚到最近交易日。
    strat = str(cfg.get("strategy", "equal_weight")).lower()
    if strat == "hype_watch":
        sig_cmd = [py_d, "scripts/live_signal_hype.py",
                   "--data-dir", cfg["data_dir"],
                   "--holdings", cfg["holdings"],
                   "--out-dir", cfg["out_dir"],
                   "--as-of", date,
                   "--position", str(cfg["position"])]
        themes = cfg.get("themes")
        if themes and str(themes).lower() not in ("null", "none", ""):
            sig_cmd += ["--themes", str(themes)]
    else:
        sig_cmd = [py_d, "scripts/live_signal.py",
                   "--data-dir", cfg["data_dir"],
                   "--holdings", cfg["holdings"],
                   "--out-dir", cfg["out_dir"],
                   "--sample", cfg["sample"],
                   "--as-of", date,
                   "--position", str(cfg["position"])]
    if cash is not None:
        sig_cmd += ["--cash", str(cash)]
    if cfg.get("ignore_quarter"):
        sig_cmd.append("--ignore-quarter")

    rc = run_step(f"live_signal[{strat}]", sig_cmd, log, timeout=600)
    if rc != 0:
        return rc

    orders = find_recent_orders(cfg)
    if orders is None:
        print(f"\n最近 24 小时没有生成 orders.csv（非季度月只检查，或 --dry-run），不需下单。")
        return 0
    print(f"\n委托文件：{orders}")

    # 3) 下单
    if args.dry_run or not cfg.get("auto_trade"):
        why = "--dry-run" if args.dry_run else "config live.yaml 的 auto_trade=false"
        print(f"\n{why}：跳过实际下单。委托文件在 {orders}")
        rc = run_step_retry("place-orders(dry-run)", [
            py_t, "scripts/gm_bridge.py", "place-orders",
            "--orders", str(orders), "--dry-run",
            "--max-orders", str(cfg["max_orders"]),
        ], log, timeout=120)
        return rc

    return run_step_retry("place-orders", [
        py_t, "scripts/gm_bridge.py", "place-orders",
        "--orders", str(orders),
        "--order-type", str(cfg["order_type"]),
        "--max-orders", str(cfg["max_orders"]),
    ], log, timeout=300)


def stage_settle(cfg: dict, log, args) -> int:
    """取成交回报 -> 对账 -> 更新持仓。"""
    py_t, py_d = cfg["python_trade"], cfg["python_data"]
    date = today()
    fills_dir = ROOT / cfg["fills_dir"]
    fills_dir.mkdir(parents=True, exist_ok=True)
    fills = fills_dir / f"{date}.csv"
    orders = find_recent_orders(cfg)

    rc = run_step_retry("pull-fills", [
        py_t, "scripts/gm_bridge.py", "pull-fills",
        "--out", str(fills),
    ], log, timeout=120)
    if rc != 0:
        return rc

    if orders is None:
        print(f"\n最近 24 小时没有委托文件（今天没调仓），跳过对账。")
        return 0

    # reconcile 自带重复入账指纹：同一批成交再跑会退出码 1。
    # 这里把 1 当作"已经对过"，不算失败；其余非零才是问题。
    rc = run_step("reconcile", [
        py_d, "scripts/reconcile.py",
        "--orders", str(orders),
        "--fills", str(fills),
        "--holdings", cfg["holdings"],
        "--save-holdings", cfg["holdings"],
        "--out", str(orders.parent),
    ], log, timeout=300, allow_fail=True)
    if rc == 1:
        print("这批成交已经入过账，跳过（正常）。")
        return 0
    return rc


STAGES = {
    "data": stage_data,
    "trade": stage_trade,
    "settle": stage_settle,
}


# ---------- 钉钉推送 ----------
def _load_notify() -> dict:
    """读 config/notify.yaml：webhook_url / secret / enabled。"""
    p = ROOT / "config" / "notify.yaml"
    if not p.exists():
        return {}
    c = load_yaml(p)
    if not c.get("enabled", True):
        return {}
    if not c.get("webhook_url"):
        return {}
    return c


def dingtalk_push(title: str, text: str, timeout: int = 10) -> bool:
    """钉钉自定义机器人（加签）。返回是否发送成功。

    失败只打印不抛：**通知挂了不能把交易也拖死**。
    """
    import base64
    import hashlib
    import hmac
    import json as _json
    import time
    import urllib.parse
    import urllib.request

    cfg = _load_notify()
    if not cfg:
        return False
    url = str(cfg["webhook_url"])
    secret = str(cfg.get("secret", ""))
    if secret:
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        hmac_code = hmac.new(secret.encode("utf-8"),
                             string_to_sign.encode("utf-8"),
                             digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url = f"{url}&timestamp={ts}&sign={sign}"

    # markdown 比 text 好读，钉钉支持
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": f"### {title}\n\n{text}"},
    }
    data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        ok = '"errcode":0' in body.replace(" ", "")
        if not ok:
            print(f"[warn] 钉钉返回非 0：{body[:200]}", flush=True)
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 钉钉推送失败：{type(e).__name__}: {e}", flush=True)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="实盘/仿真日循环主控")
    ap.add_argument("--stage", choices=["data", "trade", "settle", "all"],
                    required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="数据不写盘 / 下单不发送，全链路演练用")
    args = ap.parse_args()

    cfg = config()
    ld = log_dir(cfg)
    log_path = ld / f"{today()}.log"
    log = log_path.open("a", encoding="utf-8")
    log.write(f"\n{'#'*70}\n# live_auto stage={args.stage} dry_run={args.dry_run} "
              f"at {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n{'#'*70}\n")

    print(f"stage={args.stage}  dry_run={args.dry_run}  日志 {log_path}", flush=True)

    stages = ["data", "trade", "settle"] if args.stage == "all" else [args.stage]
    worst = 0
    results = []          # (stage, rc) 供推送
    for s in stages:
        # --dry-run 下 data 阶段只预览，避免把"演练"和"真更新"混在一起
        rc = STAGES[s](cfg, log, args)
        results.append((s, rc))
        if rc != 0:
            worst = rc
            if s != "settle":     # settle 允许"已入账"软失败
                break
    log.write(f"\n[live_auto] done rc={worst}\n")
    log.close()

    # ---- 推送结果（失败不阻断）----
    if not args.dry_run:
        ok = worst == 0
        title = ("[量化] " + ("✅ " if ok else "❌ ")
                 + f"{args.stage} {'成功' if ok else '失败 rc=' + str(worst)}")
        lines = [f"**stage**: {args.stage}",
                 f"**时间**: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
                 f"**结果**: {'成功' if ok else '失败 rc=' + str(worst)}"]
        for s, rc in results:
            lines.append(f"- {s}: {'✓' if rc == 0 else '✗ rc=' + str(rc)}")
        lines.append(f"\n日志 `{log_path.name}`")
        if not ok:
            lines.append("\n**请尽快查看日志**")
        dingtalk_push(title, "\n".join(lines))
    return worst


if __name__ == "__main__":
    sys.exit(main())
