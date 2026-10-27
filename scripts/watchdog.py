"""独立看门狗：站在流水线外面，核对「该跑的任务真的跑了」。

为什么必须独立于 live_auto
--------------------------
2026-10-26/27 三天：计划任务里脚本写的是相对路径，任务以 System32 为工作目录
启动，Python 报 `can't open file` 退出码 2——脚本一行都没跑，日志没写、
钉钉没推、一切静悄悄。**进程内告警的致命盲区就是"进程没起来"**，
只有外置看门狗能发现"到点了却没有心跳"。

机制
----
* 心跳：`live_auto` 每个 stage 结束写 `live/heartbeat.json`
  （`{日期: {stage: {rc, at, dry_run}}}`）。只有 `dry_run=false` 的心跳
  才算"真的跑过"——演练不能把坏掉的计划任务掩盖掉。
* 巡检：到点 + 宽限后仍无心跳 → 告警；有心跳但 rc≠0 → 兜底告警
  （live_auto 自己会推，这里是防它推送失败的第二只眼）。
* 去重：同一 (日期, stage, 故障类型) 只报一次，记在 `live/_watchdog_state.json`。
* 存活证明：每个交易日 16:00 后发一条当日巡检汇总——看门狗自己死了，
  这条汇总也会缺席，人眼可查。

用法
----
    python scripts/watchdog.py                    # 巡检一次
    python scripts/watchdog.py --dry-run          # 只看判定，不推送
    python scripts/watchdog.py --now "2026-10-28 10:00"   # 回放判定（测试用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_live_auto():
    """复用 live_auto 的钉钉推送与配置读取，不复制一份密钥逻辑。"""
    p = Path(__file__).resolve().parent / "live_auto.py"
    spec = importlib.util.spec_from_file_location("live_auto_for_watchdog", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# stage -> (计划时刻, 宽限分钟)。与 deploy/register_tasks.ps1 保持一致。
SCHEDULE: dict[str, tuple[str, int]] = {
    "data": ("08:30", 30),
    "trade": ("09:15", 30),
    "settle": ("15:35", 30),
}
SUMMARY_AFTER = "16:00"          # 每日汇总的最早发出时间
STATE_FILE = "live/_watchdog_state.json"
HEARTBEAT_FILE = "live/heartbeat.json"


def _at(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = hhmm.split(":")
    return dt.datetime.combine(day, dt.time(int(h), int(m)))


def check(now: dt.datetime, heartbeats: dict, alerted: dict,
          summary_sent: set) -> tuple[list[dict], dict, set]:
    """巡检判定。纯函数：输入现状，输出告警与新去重状态，方便单测回放。

    告警项 {"stage", "kind", "text"}；kind ∈ missing / failed。
    """
    alerts: list[dict] = []
    if now.weekday() >= 5:                      # 周末没有计划任务
        return alerts, alerted, summary_sent
    day = now.date().isoformat()
    dayrec = heartbeats.get(day, {}) or {}

    for stage, (hhmm, grace) in SCHEDULE.items():
        deadline = _at(now.date(), hhmm) + dt.timedelta(minutes=grace)
        if now < deadline:
            continue                             # 还没到点，不判
        rec = dayrec.get(stage)
        if rec is None or rec.get("dry_run"):
            kind, detail = "missing", "到点没有心跳（任务没跑，或跑的是演练）"
        elif int(rec.get("rc", 0)) != 0:
            kind, detail = "failed", f"跑了但失败 rc={rec.get('rc')}"
        else:
            continue                             # 正常，不报
        key = f"{day}|{stage}"
        if alerted.get(key) == kind:
            continue                             # 同类告警不重复轰炸
        alerted[key] = kind
        alerts.append({
            "stage": stage, "kind": kind,
            "text": (f"**{stage}**（计划 {hhmm}，宽限 {grace} 分钟）：{detail}\n"
                     f"**现在**: {now:%Y-%m-%d %H:%M}\n"
                     f"查 `live/logs/{day}.log` 与计划任务「a_share_quant_{stage}」"),
        })

    # ---- 每日汇总（16:00 后发一次）：既是当日体检，也是看门狗的存活证明 ----
    if now >= _at(now.date(), SUMMARY_AFTER) and day not in summary_sent:
        summary_sent.add(day)
        rows = []
        for stage, (hhmm, _) in SCHEDULE.items():
            rec = dayrec.get(stage)
            if rec is None:
                rows.append(f"- {stage}: ❌ 无心跳")
            elif rec.get("dry_run"):
                rows.append(f"- {stage}: ⚠️ 只有演练心跳")
            elif int(rec.get("rc", 0)) != 0:
                rows.append(f"- {stage}: ❌ rc={rec.get('rc')}")
            else:
                rows.append(f"- {stage}: ✓ {rec.get('at', '')[11:16]}")
        alerts.append({"stage": "summary", "kind": "summary",
                       "text": (f"**巡检汇总** {day}\n" + "\n".join(rows)
                                + "\n\n（本条由看门狗发出，收到即说明监控本身活着）")})
    return alerts, alerted, summary_sent


def main() -> int:
    ap = argparse.ArgumentParser(description="量化流水线独立看门狗")
    ap.add_argument("--now", default=None,
                    help="判定时刻 YYYY-MM-DD HH:MM（测试回放用，默认当前时间）")
    ap.add_argument("--dry-run", action="store_true", help="只判定不推送")
    args = ap.parse_args()

    now = (dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now
           else dt.datetime.now())

    hb_path = ROOT / HEARTBEAT_FILE
    heartbeats = {}
    if hb_path.exists():
        try:
            heartbeats = json.loads(hb_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            print(f"[warn] 心跳文件读不了（{e}），按全缺处理", flush=True)

    state_path = ROOT / STATE_FILE
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            state = {}
    alerted = dict(state.get("alerted", {}))
    summary_sent = set(state.get("summary_sent", []))

    alerts, alerted, summary_sent = check(now, heartbeats, alerted, summary_sent)

    if not alerts:
        print(f"{now:%Y-%m-%d %H:%M} 巡检正常", flush=True)
    else:
        la = _load_live_auto()
        for a in alerts:
            title = "[量化][看门狗] " + ("✅ 每日巡检" if a["kind"] == "summary"
                                        else f"⚠️ {a['stage']} {a['kind']}")
            print(f"\n--- {title} ---\n{a['text']}", flush=True)
            if not args.dry_run:
                la.dingtalk_push(title, a["text"])

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"alerted": alerted, "summary_sent": sorted(summary_sent)},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
