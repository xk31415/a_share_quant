"""掘金仿真交易桥接：用 gm SDK 策略模式连本地终端。

为什么只能用 gm SDK
-------------------
掘金终端（3.24.2.1）的交易接口只对 `gm` SDK 的 `run()` 策略上下文开放；
普通脚本直接调交易函数会挂死（实测 `get_cash()` 305 秒无返回）。

架构
----
`gm.api.run()` 是事件循环，交易函数只能在策略上下文里调。
所以每次操作都是：写临时策略模块 → 子进程 `run()` 执行 → 读结果 JSON → 删临时文件。
子进程隔离，避免 gm 的事件循环污染调用方。

执行日涨跌停闸门（place-orders）
--------------------------------
下单前按**执行日**判定，与 `backtest/execution.py` 同口径：
  * 现价（盘前 = 集合竞价参考价）≥ 当日涨停价 → `blocked:limit_up`，不追买
  * 现价 ≤ 当日跌停价 → `blocked:limit_down`，不贱卖
  * 停牌 → `blocked:suspended`；涨跌停价缺失 → `blocked:no_limit_data`；
    行情缺失 → `blocked:no_quote`。缺数据一律不放行（AGENTS.md 硬规则 7）。
  * 判定不看是否持有——`execution.py` 对触板的买单一律拦，加仓也一样。
当日涨跌停价取 `get_history_instruments` 的官方 `upper_limit` / `lower_limit`，
**不自己推算**（ST / 创业板 / 新股 / 主板 ST 新规各不相同）。

闸门结果落两份文件（与委托同目录）：
  `placement.csv`         实际发送的委托（对账用它，成交率才有意义）
  `placement_skipped.csv` 被拦下的委托 + 原因
`orders.csv` 是策略意图，不改动。

symbol 编码
-----------
内部用 6 位纯代码 `000001`；掘金用 `SZSE.000001` / `SHSE.600000`。

用法
----
    python scripts/gm_bridge.py status
    python scripts/gm_bridge.py pull-holdings --out live/holdings.csv
    python scripts/gm_bridge.py place-orders --orders live/<日期>/orders.csv
    python scripts/gm_bridge.py pull-fills --out fills/<日期>.csv
    python scripts/gm_bridge.py cancel-all
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import subprocess
import sys
import uuid
from pathlib import Path


# ---- 账户配置 ----
def _load_account() -> tuple[str, str]:
    root = Path(__file__).resolve().parents[1]
    p = root / "config" / "gm_account.yaml"
    if not p.exists():
        raise SystemExit(f"账户配置不存在：{p}\n格式：\n  token: xxx\n  account_id: xxx")
    tok, acc = "", ""
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("token:"):
            tok = line.split(":", 1)[1].strip().strip("'\"")
        elif line.startswith("account_id:"):
            acc = line.split(":", 1)[1].strip().strip("'\"")
    if not tok or not acc:
        raise SystemExit(f"{p} 缺 token 或 account_id")
    return tok, acc


# ---- symbol 换算 ----
def to_gm_symbol(code: str) -> str:
    c = str(code).strip()
    if "." in c:
        return c
    if len(c) != 6 or not c.isdigit():
        raise ValueError(f"不是 6 位证券代码：{code!r}")
    if c[0] in ("6", "9"):
        return f"SHSE.{c}"
    if c[0] in ("0", "1", "2", "3"):
        return f"SZSE.{c}"
    if c[0] in ("4", "8"):
        return f"BJSE.{c}"
    raise ValueError(f"无法判断交易所：{code!r}")


def from_gm_symbol(sym: str) -> str:
    """`SZSE.000001` -> `000001`。仅作公开换算 API 供测试锁定；
    生产代码一律内联 `.split(".")[-1]`——生成的子进程策略模块里没有本模块的
    命名空间，引用它必 NameError（2026-10-27 评审 P0-1）。"""
    s = str(sym).strip()
    return s.split(".", 1)[1] if "." in s else s


# ---- 在 gm 策略上下文里执行操作 ----
_ROOT = Path(__file__).resolve().parents[1]
_TMP_DIR = _ROOT / "live" / "_tmp"


def _run_in_context(body: str, timeout: int = 120) -> dict:
    tok, acc = _load_account()
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    mod_name = f"_gmop_{tag}"
    result_file = _TMP_DIR / f"{mod_name}.json"
    mod_file = _TMP_DIR / f"{mod_name}.py"

    code = (
        'import json, os, sys, traceback\n'
        f'sys.path.insert(0, r"{_ROOT}")\n'
        'from gm.api import *\n'
        f'ACC_ID = "{acc}"\n'
        'result = {"ok": False, "error": ""}\n'
        '\n'
        'def init(context):\n'
        '    global result\n'
        '    set_account_id(ACC_ID)\n'
        '    try:\n'
        f'{body}\n'
        '        result["ok"] = True\n'
        '    except Exception as e:\n'
        '        result["error"] = f"{type(e).__name__}: {e}"\n'
        '        traceback.print_exc()\n'
        '    finally:\n'
        f'        with open(r"{result_file}", "w", encoding="utf-8") as f:\n'
        '            json.dump(result, f, ensure_ascii=False, default=str)\n'
        '        stop()\n'
        '\n'
        # token 走环境变量，不进临时文件——进程被强杀时文件会残留（评审 P1-2）
        'run(filename="' + mod_name + '", mode=MODE_LIVE, token=os.environ.get("GM_TOKEN", ""))\n'
    )
    mod_file.write_text(code, encoding="utf-8")

    data = {}
    out_tail = ""
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "GM_TOKEN": tok}
        p = subprocess.run([sys.executable, str(mod_file)], cwd=str(_ROOT),
                           timeout=timeout, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=env)
        for ln in (p.stdout + p.stderr).splitlines():
            if any(k in ln for k in ("WARNING", "ERROR", "Error", "FAIL")):
                print(ln, flush=True)
        if result_file.exists():
            data = json.loads(result_file.read_text(encoding="utf-8"))
        if not data:
            # 进程没写结果文件 = gm 会话根本没起来（终端重启/掉线/代码在 init 前崩）。
            # 不能返回空 dict 让上层报 "error: None"——把子进程尾部带出来才查得动。
            tail = (p.stdout + p.stderr).strip().splitlines()[-20:]
            out_tail = "；".join(tail) or f"退出码 {p.returncode}，无输出"
            data = {"ok": False,
                    "error": f"gm 会话未返回结果（终端掉线或重启？）：{out_tail}"}
    except subprocess.TimeoutExpired:
        raise SystemExit(f"gm 策略执行超时（>{timeout}s）")
    finally:
        result_file.unlink(missing_ok=True)
        mod_file.unlink(missing_ok=True)

    return data


# ---- 执行日涨跌停闸门 ----
# 与 backtest/execution.py 逐条对齐（那边用执行日 open 对执行日涨跌停价）。
# 实盘在 09:30:05 下单（开盘后 5 秒），现价已含开盘信息：
# 封板时现价就是板价，判得出来；没封板就放行——与回测"t+1 开盘没触板可买"一致。
# （2026-10-28 之前是 09:15 下单、拿集合竞价参考价当代理，已改。）
LIMIT_EPS = 1e-9

# 数据缺失类拦截：不是"今天买不进"，是"根本没拿到判定依据"。
# 全被这类拦下时必须让上层知道（退出码 2），否则 stage_trade 拿 0 当成功、
# 钉钉推"✅ 成功"，而实际一笔没发（2026-10-27 评审 P1-A）。
DATA_GAP_NOTES = ("blocked:no_quote", "blocked:no_limit_data")


def apply_execution_gate(orders: list[dict], quotes: dict) -> list[dict]:
    """按执行日行情拦下触板/停牌/缺数据的委托。返回新列表，不改输入。

    quotes: {6 位代码: {"price", "limit_up", "limit_down", "suspended"}}
    拦下的行变成 ``side=hold, shares=0``，``note`` 写原因，格式与
    `live_signal` 历史输出一致（对账按 hold 行为空处理）。
    """
    out: list[dict] = []
    for od in orders:
        side = (od.get("side") or "").lower()
        if side not in ("buy", "sell"):
            out.append(od)          # hold 行原样透传
            continue
        q = quotes.get(str(od.get("symbol", "")))
        note = None
        if not q:
            note = "blocked:no_quote"
        elif q.get("suspended"):
            note = "blocked:suspended"
        elif q.get("price") is None:
            note = "blocked:no_quote"
        else:
            px = float(q["price"])
            if side == "buy":
                lu = q.get("limit_up")
                if lu is None or float(lu) <= 0:
                    note = "blocked:no_limit_data"
                elif px >= float(lu) - LIMIT_EPS:
                    note = "blocked:limit_up"
            else:
                ld = q.get("limit_down")
                if ld is None or float(ld) <= 0:
                    note = "blocked:no_limit_data"
                elif px <= float(ld) + LIMIT_EPS:
                    note = "blocked:limit_down"
        if note:
            out.append({**od, "side": "hold", "shares": 0, "amount": 0.0, "note": note})
        else:
            out.append(od)
    return out


LOT_SIZE = 100


def _ceil_cent(x: float) -> float:
    """向上取整到 0.01（买入限价宁高勿低，否则挂着买不进）。"""
    return math.ceil(round(float(x), 6) * 100 - 1e-9) / 100.0


def _floor_cent(x: float) -> float:
    """向下取整到 0.01（卖出限价宁低勿高）。"""
    return math.floor(round(float(x), 6) * 100 + 1e-9) / 100.0


def apply_execution_pricing(rows: list[dict], quotes: dict, *,
                            slippage_rate: float = 0.0005,
                            cash: float | None = None,
                            cash_buffer: float = 0.0) -> tuple[list[dict], dict]:
    """把委托价换成**执行日实时价 × (1 ± 滑点)**，现金不足时按比例缩减买入股数。

    为什么不能用 orders.csv 里的价：那是**信号日收盘价**。回测
    `backtest/execution.py` 用 `price_col="open"`（t+1 开盘价 + 滑点），
    实盘挂信号日收盘价等于假设「任何票都能按昨收价买到」——系统性偏乐观。
    2026-10-28 实测（三笔全错，方向还不一致）：
      * 000678 委托 16.01，当日最低 16.66 -> 真实市场整天买不到，仿真凭空 +10%；
      * 601086 委托 12.62，开盘 12.26 -> 真实买得更便宜，仿真多付 3%；
      * 001318 委托 13.31，开盘 13.54 -> 真实要等盘中跌破才成交，不是开盘。
    仿真环境下成交价完全由我们下的限价决定（掘金按限价立即全额成交），
    所以「挂什么价」就是「成交什么价」，必须让它对齐执行日真实价格。

    规则：
      * 买入价 = 实时价 × (1 + slippage)，向上取整到分；卖出价 = × (1 - slippage)，
        向下取整到分——宁可多给一分，也不要挂着不成交；
      * 挂单价夹在当日涨跌停价之内（真实市场也不允许越界）；
      * 买入总额超预算时**按比例缩减全部买单**（与 `ExecutionSimulator` 同口径），
        不按顺序丢弃靠后的标的（那会引入与代码字典序相关的偏差）；
        缩减后不足 1 手的标的不发单；
      * 预算 = 可用现金 + 卖出预估所得（回测也是先卖后买、用释放后的资金）。

    返回 ``(新列表, 统计)``；``statistics["scaled"]`` < 1 表示发生了缩减。
    """
    out: list[dict] = []
    repriced = 0
    slip = max(0.0, float(slippage_rate))

    for od in rows:
        row = dict(od)
        side = (row.get("side") or "").lower()
        if side not in ("buy", "sell"):
            out.append(row)                     # hold 行原样透传
            continue
        q = quotes.get(str(row.get("symbol", "")).strip()) or {}
        try:
            px = float(q["price"]) if q.get("price") is not None else None
        except (TypeError, ValueError):
            px = None
        if px is None or px <= 0:
            out.append(row)                     # 闸门已按 blocked:no_quote 拦下，兜底透传
            continue

        if side == "buy":
            limit_px = _ceil_cent(px * (1.0 + slip))
            up = q.get("limit_up")
            if up is not None and float(up) > 0:
                limit_px = min(limit_px, round(float(up), 2))
        else:
            limit_px = _floor_cent(px * (1.0 - slip))
            dn = q.get("limit_down")
            if dn is not None and float(dn) > 0:
                limit_px = max(limit_px, round(float(dn), 2))

        try:
            old_px = float(row.get("price") or 0.0)
        except (TypeError, ValueError):
            old_px = 0.0
        if abs(limit_px - old_px) > 1e-9:
            repriced += 1
        row["price"] = limit_px
        row["amount"] = round(int(float(row.get("shares") or 0)) * limit_px, 2)
        out.append(row)

    scaled = 1.0
    dropped = 0
    buys = [r for r in out
            if (r.get("side") or "").lower() == "buy" and float(r.get("shares") or 0) > 0]
    if buys and cash is not None:
        budget = float(cash) * (1.0 - max(0.0, float(cash_buffer)))
        budget += sum(int(float(r.get("shares") or 0)) * float(r.get("price") or 0.0)
                      for r in out if (r.get("side") or "").lower() == "sell")
        need = sum(int(float(r["shares"])) * float(r["price"]) for r in buys)
        if need > budget:
            scaled = budget / need if budget > 0 else 0.0
            for r in buys:
                lots = int(int(float(r["shares"])) * scaled) // LOT_SIZE * LOT_SIZE
                if lots <= 0:
                    dropped += 1
                r["shares"] = lots
                r["amount"] = round(lots * float(r["price"]), 2)

    return out, {"repriced": repriced, "scaled": scaled, "dropped": dropped,
                 "slippage": slip}


def _fetch_execution_quotes(codes: list[str], date: str) -> dict:
    """取执行日官方涨跌停价 + 现价。取不到整个会话就直接失败，**不放行**。

    ``get_history_instruments`` 的 upper_limit/lower_limit 是交易所口径
    （build_dataset.py 的数据管道也用它），自己推算会踩 ST/创业板/新股的坑。
    """
    syms = [to_gm_symbol(c) for c in codes]
    body = (
        f'        syms = {syms!r}\n'
        f'        hi = get_history_instruments(symbols=syms, start_date="{date}", '
        f'end_date="{date}", '
        'fields="symbol,upper_limit,lower_limit,is_suspended", df=True)\n'
        '        result["limits"] = ([] if (hi is None or not len(hi)) else\n'
        '            [{"symbol": str(r["symbol"]), "upper_limit": None if r["upper_limit"] != r["upper_limit"] else float(r["upper_limit"]),\n'
        '              "lower_limit": None if r["lower_limit"] != r["lower_limit"] else float(r["lower_limit"]),\n'
        '              "is_suspended": bool(r.get("is_suspended", False))}\n'
        '             for r in hi.to_dict("records")])\n'
        '        cur = current(syms)\n'
        '        result["current"] = []\n'
        '        for x in (cur or []):\n'
        '            d = dict(x)\n'
        '            result["current"].append({"symbol": str(d.get("symbol", "")),\n'
        '                "price": d.get("price"), "open": d.get("open")})\n'
    )
    data = _run_in_context(body, timeout=180)
    if not data.get("ok"):
        raise SystemExit(
            f"取执行日行情失败，涨跌停闸门无法判定——**拒绝下单**：{data.get('error')}")

    quotes: dict[str, dict] = {}
    for rec in data.get("limits", []):
        code = str(rec.get("symbol", "")).split(".")[-1]
        quotes[code] = {"price": None, "suspended": bool(rec.get("is_suspended")),
                        "limit_up": rec.get("upper_limit"),
                        "limit_down": rec.get("lower_limit")}
    for rec in data.get("current", []):
        code = str(rec.get("symbol", "")).split(".")[-1]
        px = rec.get("price")
        try:
            px = float(px) if px not in (None, "") else None
        except (TypeError, ValueError):
            px = None
        if px is None:                      # 盘前 price 可能为 0/空，退回开盘参考
            try:
                px = float(rec.get("open") or 0) or None
            except (TypeError, ValueError):
                px = None
        quotes.setdefault(code, {"suspended": False,
                                 "limit_up": None, "limit_down": None})
        quotes[code]["price"] = px
    return quotes


# ---- 查询 ----
def cmd_status(args) -> int:
    body = '''        cash = get_cash()
        pos = get_position()
        result["cash"] = {
            "nav": float(getattr(cash, "nav", 0) or 0),
            "available": float(getattr(cash, "available", 0) or 0),
        }
        result["positions"] = []
        for p in (pos or []):
            if isinstance(p, dict):
                g = p.get
            else:
                g = lambda k, d=None: getattr(p, k, d)
            result["positions"].append({
                "symbol": str(g("symbol", g("security_id", ""))).split(".")[-1],
                "volume": int(float(g("volume", g("position", 0)) or 0)),
                "cost_price": float(g("cost_price", g("avg_cost", 0)) or 0),
                "price": float(g("price", g("last_price", 0)) or 0),
            })
        result["account_id"] = ACC_ID'''
    data = _run_in_context(body)
    if not data.get("ok"):
        raise SystemExit(f"查询失败：{data.get('error')}")
    c = data.get("cash", {})
    print(f"账户 {data.get('account_id', '?')}")
    print(f"  净值 nav        {c.get('nav', 0):>16,.2f}")
    print(f"  可用 available  {c.get('available', 0):>16,.2f}")
    positions = data.get("positions", [])
    print(f"  持仓 {len(positions)} 只")
    for p in positions[:10]:
        print(f"    {p['symbol']}  {p['volume']:>8d} 股  "
              f"成本 {p['cost_price']:>8.2f}  现价 {p['price']:>8.2f}")
    if len(positions) > 10:
        print(f"    ... 还有 {len(positions) - 10} 只")
    return 0


# ---- 持仓导出 ----
def cmd_pull_holdings(args) -> int:
    body = '''        cash = get_cash()
        pos = get_position()
        result["available"] = float(getattr(cash, "available", 0) or 0)
        result["holdings"] = []
        for p in (pos or []):
            if isinstance(p, dict):
                g = p.get
            else:
                g = lambda k, d=None: getattr(p, k, d)
            sym = str(g("symbol", g("security_id", ""))).split(".")[-1]
            vol = int(float(g("volume", g("position", 0)) or 0))
            if vol > 0:
                result["holdings"].append([sym, vol])'''
    data = _run_in_context(body)
    if not data.get("ok"):
        raise SystemExit(f"拉取持仓失败：{data.get('error')}")
    rows = sorted(data.get("holdings", []))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "shares"])
        w.writerows(rows)
    avail = data.get("available", 0)
    print(f"持仓 {len(rows)} 只 -> {out}")
    print(f"现金 available = {avail:,.2f}")
    if args.cash_out:
        cp = Path(args.cash_out)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(f"{avail:.2f}\n", encoding="utf-8")
        print(f"现金 -> {cp}")
    return 0


# ---- 下单 ----
ORDER_COLUMNS = ["symbol", "side", "shares", "price", "amount", "note"]


def _write_orders_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ORDER_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ORDER_COLUMNS})


def cmd_place_orders(args) -> int:
    orders_path = Path(args.orders)
    if not orders_path.exists():
        raise SystemExit(f"委托文件不存在：{orders_path}")

    with orders_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if (r.get("side") or "").strip() in ("buy", "sell")]

    if not rows:
        print("没有可下单的行（空或全是 hold）")
        return 0

    if args.max_orders and len(rows) > args.max_orders:
        raise SystemExit(
            f"委托 {len(rows)} 笔超过 --max-orders {args.max_orders}。"
            f"确认无误加 --max-orders {len(rows)}。")

    use_market = args.order_type == "market"
    print(f"{'[dry-run] ' if args.dry_run else ''}"
          f"共 {len(rows)} 笔，{'市价' if use_market else '限价'}"
          f"{'，先卖后买' if args.sells_first else ''}")

    ordered = rows
    if args.sells_first:
        ordered = sorted(rows, key=lambda r: 0 if (r.get("side") or "").lower() == "sell" else 1)

    # ---- 执行日涨跌停闸门（口径见模块 docstring）----
    codes = sorted({str(r["symbol"]).strip() for r in ordered})
    quotes = _fetch_execution_quotes(codes, dt.date.today().isoformat())
    gated = apply_execution_gate(ordered, quotes)
    sent = [r for r in gated if (r.get("side") or "").lower() in ("buy", "sell")]
    skipped = [r for r in gated if (r.get("side") or "").lower() == "hold"]

    reasons: dict[str, int] = {}
    for r in skipped:
        k = str(r.get("note", "blocked:?"))
        reasons[k] = reasons.get(k, 0) + 1
    print(f"执行日闸门（{dt.date.today().isoformat()}）：放行 {len(sent)} 笔，"
          f"拦下 {len(skipped)} 笔"
          + ("（" + "，".join(f"{k} {v}" for k, v in sorted(reasons.items())) + "）"
             if reasons else ""))
    # 拦下那行的股数已被清零，意图要按位置从原始行取（apply_execution_gate 保序）
    for orig, g in zip(ordered, gated):
        if (g.get("side") or "").lower() != "hold":
            continue
        print(f"  拦下 {orig.get('symbol')} {g.get('note', '')} "
              f"（意图 {orig.get('shares', 0)} 股 @ {orig.get('price', '')}）", flush=True)

    # 数据缺口：不是"今天买不进"，是"根本没拿到判定依据"。数量与放行笔数无关——
    # 只有一部分票取不到行情时同样是静默失败（2026-10-27 评审 P2-4）。
    gap = [r for r in skipped if str(r.get("note", "")) in DATA_GAP_NOTES]
    gap_reasons: dict[str, int] = {}
    for r in gap:
        k = str(r.get("note", "blocked:?"))
        gap_reasons[k] = gap_reasons.get(k, 0) + 1
    gap_desc = "，".join(f"{k} {v}" for k, v in sorted(gap_reasons.items()))

    # ---- 执行日实时价定价（2026-10-28 方案 A，口径见 apply_execution_pricing）----
    # 委托价从「信号日收盘」换成「执行日实时价 ×(1±滑点)」，与回测的
    # price_col="open" + 滑点对齐；现金不足按比例缩减股数。
    sent, pstats = apply_execution_pricing(
        sent, quotes,
        slippage_rate=float(getattr(args, "slippage", 0.0005)),
        cash=None if getattr(args, "cash", None) is None else float(args.cash))
    if pstats["repriced"]:
        print(f"执行日定价：{pstats['repriced']}/{len(sent)} 笔改为实时价"
              f"×(1±{pstats['slippage']:.4%})（orders.csv 的价只是信号日意图）")
    if pstats["scaled"] < 1.0:
        print(f"[warn] 现金不足，买入股数按比例缩减到 {pstats['scaled']:.1%}"
              f"（{pstats['dropped']} 笔不足 1 手，不发单）", flush=True)

    if args.dry_run:
        for r in sent:
            code = str(r["symbol"]).strip()
            shares = int(float(r["shares"]))
            price = float(r["price"]) if r.get("price") not in (None, "", "nan") else 0.0
            print(f"  {code} {r.get('side', '')} {shares} 股 @ {price:.2f} -> 不发送")
        if gap:
            print(f"[warn] {len(gap)} 笔因行情/涨跌停价缺失被拦下——"
                  f"真实下单时这批会触发退出码 2", flush=True)
        print("[dry-run] 不写 placement.csv（它只记录真实发送的委托）")
        return 0

    if not sent:
        if gap:
            print(f"\n闸门拦下全部委托，其中 {len(gap)} 笔是**行情/涨跌停价缺失**"
                  f"（{gap_desc}）。"
                  f"\n这不是'今天买不进'，是数据没取到——退出码 2 交人工。", flush=True)
            return 2
        # 全是触板/停牌：业务上今天就该买不进，属于正常结果
        print("闸门拦下全部委托（全部触板/停牌，属正常业务），无单可发", flush=True)
        return 0

    # shares<=0 的行不发单，也不能进 placement.csv——它是对账口径，记进去等于
    # "该成交而没成交"，白白拉低成交率（2026-10-27 探针实测：0 股那笔照常写盘）。
    sendable: list[dict] = []
    for r in sent:
        if int(float(r["shares"])) <= 0:
            print(f"  跳过 {r.get('symbol')}：shares={r.get('shares')}（<=0 不发单）",
                  flush=True)
            continue
        sendable.append(r)
    if not sendable:
        print("放行的委托股数全为 0，无单可发", flush=True)
        return 0
    sent = sendable

    # placement.csv = 真实发送的委托；对账以它为口径，成交率才不含"根本没发"的单。
    placement = Path(args.placement) if args.placement else orders_path.parent / "placement.csv"
    _write_orders_csv(placement, sent)
    _write_orders_csv(orders_path.parent / "placement_skipped.csv", skipped)
    print(f"已发送委托 -> {placement}")

    # 构造策略 body：逐笔下单
    order_parts = []
    for r in sent:
        code = str(r["symbol"]).strip()
        shares = int(float(r["shares"]))
        price = float(r["price"]) if r.get("price") not in (None, "", "nan") else 0.0
        is_buy = (r.get("side") or "").lower() == "buy"
        gm_sym = to_gm_symbol(code)
        if shares <= 0:
            print(f"  跳过 {code}：shares={shares}（<=0 不发单）", flush=True)
            continue
        # 枚举常量在生成模块里由 `from gm.api import *` 导入；用名字不用数字，
        # SDK 改枚举值不至于静默下错单（评审 P2-1，值已核对：Buy=1/Sell=2/
        # Limit=1/Market=2/Open=1/Close=2/GFD=3）
        side_c = "OrderSide_Buy" if is_buy else "OrderSide_Sell"
        type_c = "OrderType_Market" if use_market else "OrderType_Limit"
        effect_c = "PositionEffect_Open" if is_buy else "PositionEffect_Close"
        px = 0 if use_market else price
        order_parts.append(
            f'        try:\n'
            f'            ret = order_volume(symbol="{gm_sym}", volume={shares},\n'
            f'                side={side_c}, order_type={type_c},\n'
            f'                position_effect={effect_c}, price={px},\n'
            f'                order_duration=OrderDuration_GFD)\n'
            f'            result.setdefault("orders", []).append({{"symbol": "{code}", "ret": str(ret)[:100]}})\n'
            f'        except Exception as e:\n'
            f'            result.setdefault("orders", []).append({{"symbol": "{code}", "error": f"{{type(e).__name__}}: {{e}}"}})\n'
        )

    body = ''.join(order_parts)
    data = _run_in_context(body, timeout=300)
    if not data.get("ok"):
        raise SystemExit(f"下单失败：{data.get('error')}")

    errors = 0
    for od in data.get("orders", []):
        if "error" in od:
            print(f"  {od['symbol']} -> **异常** {od['error']}")
            errors += 1
        else:
            print(f"  {od['symbol']} -> {od['ret']}")

    # 两类问题都要人工：委托被拒（部分成功）与数据缺口（票根本没判）。任一存在
    # 都不能返回 0——stage_trade 会把 0 当成功推"✅"，缺口就彻底被淹没。
    problems = []
    if errors:
        problems.append(f"{errors} 笔委托异常")
    if gap:
        problems.append(f"{len(gap)} 笔因行情/涨跌停价缺失未发（{gap_desc}）")
    if problems:
        print(f"\n[需人工] {'；'.join(problems)}"
              + (f"。已发出 {len(sent)} 笔，照常进入对账" if sent else "")
              + "。", flush=True)
        return 2
    return 0


# ---- 成交回报 ----
def cmd_pull_fills(args) -> int:
    body = '''        rpts = get_execution_reports()
        result["fills"] = []
        for r in (rpts or []):
            if isinstance(r, dict):
                g = r.get
            else:
                g = lambda k, d=None: getattr(r, k, d)
            sym = str(g("symbol", g("security_id", ""))).split(".")[-1]
            side_raw = str(g("side", g("ord_side", "")) or "").lower()
            if "buy" in side_raw or side_raw in ("1",):
                side = "buy"
            elif "sell" in side_raw or side_raw in ("2",):
                side = "sell"
            else:
                side = "buy" if "open" in str(g("position_effect", "")).lower() else "sell"
            vol = int(float(g("volume", g("last_shares", 0)) or 0))
            px = float(g("price", g("last_px", 0)) or 0)
            fee = float(g("fee", g("commission", 0)) or 0)
            if vol > 0:
                result["fills"].append([sym, side, vol, px, fee])'''
    data = _run_in_context(body)
    if not data.get("ok"):
        raise SystemExit(f"取成交失败：{data.get('error')}")

    fills = data.get("fills", [])
    agg: dict = {}
    for sym, side, vol, px, fee in fills:
        k = (sym, side, f"{px:.4f}")
        a = agg.setdefault(k, [0, 0.0])
        a[0] += vol
        a[1] += fee

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "side", "shares", "price", "fee"])
        for (sym, side, px), (vol, fee) in sorted(agg.items()):
            w.writerow([sym, side, vol, px, f"{fee:.2f}"])

    print(f"成交 {len(fills)} 条 -> 聚合 {len(agg)} 行 -> {out}")
    if not agg:
        print("[warn] 今天没有成交（空回报），reconcile 会按空批次处理")
    return 0


# ---- 紧急撤单 ----
def cmd_cancel_all(args) -> int:
    if args.dry_run:
        print("[dry-run] 不撤")
        return 0
    body = '''        ret = order_cancel_all()
        result["cancel_ret"] = str(ret)[:200]'''
    data = _run_in_context(body)
    if not data.get("ok"):
        raise SystemExit(f"撤单失败：{data.get('error')}")
    print(f"撤单结果：{data.get('cancel_ret')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="掘金仿真交易桥接（gm 策略模式）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="看资金与持仓")

    p = sub.add_parser("pull-holdings", help="导出当前持仓为 holdings.csv")
    p.add_argument("--out", default="live/holdings.csv")
    p.add_argument("--cash-out", default=None, help="把可用现金另存一个文本文件")

    p = sub.add_parser("place-orders", help="按 orders.csv 下单")
    p.add_argument("--orders", required=True)
    p.add_argument("--placement", default=None,
                   help="实际发送委托的落盘路径（默认 <orders 目录>/placement.csv）")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--order-type", choices=["limit", "market"], default="limit")
    p.add_argument("--sells-first", action="store_true", default=True)
    p.add_argument("--no-sells-first", dest="sells_first", action="store_false")
    p.add_argument("--max-orders", type=int, default=200)
    p.add_argument("--slippage", type=float, default=0.0005,
                   help="执行日定价滑点：买入 ×(1+s)、卖出 ×(1-s)，与回测 cost_model 同义")
    p.add_argument("--cash", type=float, default=None,
                   help="账户可用现金；给了就按预算缩减买入股数（比例缩减，与回测同口径）")

    p = sub.add_parser("pull-fills", help="导出当日成交回报")
    p.add_argument("--out", required=True)

    p = sub.add_parser("cancel-all", help="撤销全部未完成委托")
    p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    fn = {
        "status": cmd_status,
        "pull-holdings": cmd_pull_holdings,
        "place-orders": cmd_place_orders,
        "pull-fills": cmd_pull_fills,
        "cancel-all": cmd_cancel_all,
    }[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
