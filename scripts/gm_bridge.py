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
# 实盘在 09:15 下单，开盘价还没出来，用现价（集合竞价参考价）作代理：
# 一字板时参考价就是板价，判得出来；没封板就放行——与回测"t+1 开盘没触板可买"一致。
LIMIT_EPS = 1e-9


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

    if args.dry_run:
        for r in sent:
            code = str(r["symbol"]).strip()
            shares = int(float(r["shares"]))
            price = float(r["price"]) if r.get("price") not in (None, "", "nan") else 0.0
            print(f"  {code} {r.get('side', '')} {shares} 股 @ {price:.2f} -> 不发送")
        print("[dry-run] 不写 placement.csv（它只记录真实发送的委托）")
        return 0

    if not sent:
        print("闸门拦下全部委托，无单可发", flush=True)
        return 0

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

    if errors:
        print(f"\n{errors} 笔异常，需要人工看日志", flush=True)
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
