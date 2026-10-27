"""掘金仿真交易桥接：用 gm SDK 策略模式连本地终端。

为什么不是 gmtrade
------------------
`gmtrade 3.0.6` 与掘金终端 `3.24.2.1`（2026-10-26 自动升级）不兼容，
登录报 `account登陆出错` + `1010: 无法获取掘金服务器地址列表`。
`gm` SDK 的 `run()` 策略模式实测兼容，交易函数在 `init()` 里调用即可。

架构
----
`gm.api.run()` 是事件循环，交易函数只能在策略上下文里调。
所以每次操作都是：写临时策略模块 → 子进程 `run()` 执行 → 读结果 JSON → 删临时文件。
子进程隔离，避免 gm 的事件循环污染调用方。

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
    s = str(sym).strip()
    return s.split(".", 1)[1] if "." in s else s


# ---- protobuf 兼容取值 ----
def _g(obj, *keys, default=None):
    for k in keys:
        if obj is None:
            continue
        if isinstance(obj, dict):
            if k in obj and obj[k] is not None:
                return obj[k]
        else:
            v = getattr(obj, k, None)
            if v is not None:
                return v
    return default


def _num(obj, *keys, default=0.0) -> float:
    v = _g(obj, *keys, default=default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


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
        'import json, sys, traceback\n'
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
        f'run(filename="{mod_name}", mode=MODE_LIVE, token="{tok}")\n'
    )
    mod_file.write_text(code, encoding="utf-8")

    data = {}
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        p = subprocess.run([sys.executable, str(mod_file)], cwd=str(_ROOT),
                           timeout=timeout, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=env)
        for ln in (p.stdout + p.stderr).splitlines():
            if any(k in ln for k in ("WARNING", "ERROR", "Error", "FAIL")):
                print(ln, flush=True)
        if result_file.exists():
            data = json.loads(result_file.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        raise SystemExit(f"gm 策略执行超时（>{timeout}s）")
    finally:
        result_file.unlink(missing_ok=True)
        mod_file.unlink(missing_ok=True)

    return data


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
                "symbol": from_gm_symbol(str(g("symbol", g("security_id", "")))),
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
            sym = from_gm_symbol(str(g("symbol", g("security_id", ""))))
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

    if args.dry_run:
        for r in ordered:
            code = str(r["symbol"]).strip()
            shares = int(float(r["shares"]))
            price = float(r["price"]) if r.get("price") not in (None, "", "nan") else 0.0
            side = r.get("side", "")
            print(f"  {code} {side} {shares} 股 @ {price:.2f} -> 不发送")
        return 0

    # 构造策略 body：逐笔下单
    order_parts = []
    for r in ordered:
        code = str(r["symbol"]).strip()
        shares = int(float(r["shares"]))
        price = float(r["price"]) if r.get("price") not in (None, "", "nan") else 0.0
        side_num = 1 if (r.get("side") or "").lower() == "buy" else 2  # 1=buy 2=sell
        gm_sym = to_gm_symbol(code)
        type_num = 2 if use_market else 1  # 1=limit 2=market
        effect = 1 if side_num == 1 else 2  # 1=open 2=close
        px = 0 if use_market else price
        order_parts.append(
            f'        try:\n'
            f'            ret = order_volume(symbol="{gm_sym}", volume={shares},\n'
            f'                side={side_num}, order_type={type_num},\n'
            f'                position_effect={effect}, price={px},\n'
            f'                order_duration=3)\n'
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
            sym = from_gm_symbol(str(g("symbol", g("security_id", ""))))
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
