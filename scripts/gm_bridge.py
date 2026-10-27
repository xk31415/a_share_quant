"""掘金仿真交易桥接：用 gmtrade SDK 连本地终端（127.0.0.1:7001）。

为什么是 gmtrade 而不是 gm
--------------------------
`gm` 是策略 SDK，交易函数必须在 `run()` 的策略上下文里调用，在脚本顶层
调 `get_cash()` 会挂死（实测 305 秒超时）。`gmtrade` 是独立交易 SDK，
普通脚本直接调，不需要策略框架。

连接方式（实测，与文档不符）
----------------------------
文档说"接入掘金终端则填空"，实测 `set_endpoint("")` 报「无法连接到终端服务」，
必须显式写 `127.0.0.1:7001` —— 这是终端 `gmserv.json` 里的 `rpcPort`。
`set_endpoint("127.0.0.1:7002")` 会报「account登陆出错」。

Python 版本
-----------
`gmtrade` 在 PyPI 上的 wheel 只编译到 `cp310`（Python 3.10），**必须用 3.10**。
项目信号/对账那套跑在 3.12（pandas 那边），两边由 `live_auto.py` 编排。

用法
----
    python scripts/gm_bridge.py status
    python scripts/gm_bridge.py pull-holdings --out live/holdings.csv
    python scripts/gm_bridge.py place-orders --orders live/2026-10-27/orders.csv
    python scripts/gm_bridge.py place-orders --orders ... --dry-run
    python scripts/gm_bridge.py pull-fills --out fills/2026-10-27.csv
    python scripts/gm_bridge.py cancel-all

symbol 编码
-----------
内部（数据/信号/对账）用 6 位纯代码 `000001`；
掘金用 `SZSE.000001` / `SHSE.600000`。本模块负责换算。
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# ---- 账户配置 ----
# token/account_id 不进代码，从 config/gm_account.yaml 读。
def _load_account() -> tuple[str, str]:
    root = Path(__file__).resolve().parents[1]
    p = root / "config" / "gm_account.yaml"
    if not p.exists():
        raise SystemExit(f"账户配置不存在：{p}\n"
                         f"格式：\n  token: xxx\n  account_id: xxx")
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
    """`000001` -> `SZSE.000001`；已是带前缀的原样返回。"""
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
    """`SZSE.000001` -> `000001`。"""
    s = str(sym).strip()
    return s.split(".", 1)[1] if "." in s else s


# ---- 连接 ----
_account_obj = None


def connect():
    """登录掘金终端。必须先跑通这个，再调任何交易函数。

    登录失败必须显式报错退出，**不能静默继续**——
    登录态过期时 get_cash 会返回 None/0，静默走下去等于"空仓清仓"全错。
    """
    global _account_obj
    from gmtrade.api import (  # 延迟导入：本文件的 symbol 换算可独立测试
        account, get_cash, login, set_endpoint, set_token,
    )
    tok, acc = _load_account()
    set_token(tok)
    set_endpoint("127.0.0.1:7001")   # 不能留空，见模块 docstring
    _account_obj = account(account_id=acc, account_alias="live_auto")
    login(_account_obj)
    # 验证登录态：get_cash 返回 None/空 = 登录失败（gmtrade 不抛异常，只 warn）
    cash = get_cash(acc)
    if not cash or _num(cash, "nav") <= 0:
        raise SystemExit(
            f"\n掘金账户登录失败（account_id={acc}）。\n"
            f"get_cash 返回空或 nav<=0：{cash!r}\n"
            f"请检查：\n"
            f"  1. 掘金客户端（emgm3）是否已登录账号\n"
            f"  2. 登录态是否过期（周末/长时间未操作会掉）\n"
            f"  3. 网络能否连到掘金服务器\n")
    return _account_obj


def _g(obj, *keys, default=None):
    """从 dict / protobuf 对象里取值。gmtrade 返回的是 protobuf，不是 dict。

    按 keys 顺序找第一个存在的属性/键；都没有就返回 default。
    """
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


# ---- 查询 ----
def cmd_status(args) -> int:
    acc = connect()
    from gmtrade.api import get_cash, get_positions, get_unfinished_orders
    # gmtrade 的查询函数要 account_id 字符串，不是 Account 对象
    aid = acc.account_id
    cash = get_cash(aid)
    pos = get_positions(aid)
    unfin = get_unfinished_orders(aid)
    n_pos = len(pos) if pos else 0
    n_unfin = len(unfin) if unfin else 0
    print(f"账户 {_account_obj.account_id}")
    print(f"  净值 nav        {_num(cash, 'nav'):>16,.2f}")
    print(f"  可用 available  {_num(cash, 'available'):>16,.2f}")
    print(f"  冻结 frozen     {_num(cash, 'frozen', 'frozen_cash'):>16,.2f}")
    print(f"  持仓 {n_pos} 只，未完成委托 {n_unfin} 笔")
    if pos:
        for p in list(pos)[:10]:
            sym = from_gm_symbol(str(_g(p, "symbol", "security_id", default="")))
            vol = int(_num(p, "volume", "position", "current_volume"))
            cost = _num(p, "cost_price", "avg_cost", "open_price")
            px = _num(p, "price", "last_price", "market_value")
            print(f"    {sym}  {vol:>8d} 股  成本 {cost:>8.2f}  现价 {px:>8.2f}")
        if n_pos > 10:
            print(f"    ... 还有 {n_pos - 10} 只")
    return 0


# ---- 持仓导出 ----
def cmd_pull_holdings(args) -> int:
    acc = connect()
    from gmtrade.api import get_cash, get_positions
    pos = get_positions(acc.account_id) or []
    rows = []
    for p in pos:
        sym = from_gm_symbol(str(_g(p, "symbol", "security_id", default="")))
        vol = int(_num(p, "volume", "position", "current_volume"))
        if vol > 0:
            rows.append((sym, vol))
    rows.sort()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "shares"])
        w.writerows(rows)
    cash = get_cash(acc.account_id)
    avail = _num(cash, "available")
    print(f"持仓 {len(rows)} 只 -> {out}")
    print(f"现金 available = {avail:,.2f}")
    if args.cash_out:
        cp = Path(args.cash_out)
        cp.write_text(f"{avail:.2f}\n", encoding="utf-8")
        print(f"现金 -> {cp}")
    return 0


# ---- 下单 ----
def cmd_place_orders(args) -> int:
    from gmtrade.api import (
        OrderDuration_GFD, OrderSide_Buy, OrderSide_Sell,
        OrderType_Limit, OrderType_Market, PositionEffect_Close,
        PositionEffect_Open, get_unfinished_orders, order_volume,
    )

    orders_path = Path(args.orders)
    if not orders_path.exists():
        raise SystemExit(f"委托文件不存在：{orders_path}")

    # 读 orders.csv：symbol,side,shares,price,amount,note
    with orders_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if (r.get("side") or "").strip() in ("buy", "sell")]

    if not rows:
        print("没有可下单的行（空或全是 hold）")
        return 0

    if args.max_orders and len(rows) > args.max_orders:
        raise SystemExit(
            f"委托 {len(rows)} 笔超过 --max-orders {args.max_orders}。"
            f"实盘/仿真都该先看看是不是哪里错了，不要盲下。"
            f"确认无误加 --max-orders {len(rows)}。")

    use_market = args.order_type == "market"
    print(f"{'[dry-run] ' if args.dry_run else ''}"
          f"共 {len(rows)} 笔，{'市价' if use_market else '限价'}"
          f"{'，先卖后买' if args.sells_first else ''}")

    def _send(r) -> str:
        code = str(r["symbol"]).strip()
        side_raw = (r.get("side") or "").strip().lower()
        shares = int(float(r["shares"]))
        price = float(r["price"]) if r.get("price") not in (None, "", "nan") else 0.0
        if shares <= 0:
            return f"{code} 跳过（股数 {shares}）"
        gm_sym = to_gm_symbol(code)
        side = OrderSide_Buy if side_raw == "buy" else OrderSide_Sell
        effect = PositionEffect_Open if side_raw == "buy" else PositionEffect_Close
        otype = OrderType_Market if use_market else OrderType_Limit
        if args.dry_run:
            return f"{code} {side_raw} {shares} 股 @ {price:.2f} -> 不发送"
        try:
            ret = order_volume(
                symbol=gm_sym, volume=shares, side=side,
                order_type=otype, position_effect=effect,
                price=0 if use_market else price,
                order_duration=OrderDuration_GFD,
                account=_account_obj.account_id,
            )
            # order_volume 成功返回委托编号类对象，失败返回错误码(int)或 dict
            return f"{code} {side_raw} {shares} @ {price:.2f} -> {ret}"
        except Exception as e:  # noqa: BLE001 下单失败必须让人看见，不能静默
            return f"{code} {side_raw} {shares} @ {price:.2f} -> **异常** {type(e).__name__}: {e}"

    # 先卖后买：卖出腾出资金，避免买单被"可用不足"卡住
    ordered = rows
    if args.sells_first:
        ordered = sorted(rows, key=lambda r: 0 if (r.get("side") or "").lower() == "sell" else 1)

    errors = 0
    for r in ordered:
        line = _send(r)
        print("  " + line, flush=True)
        if "**异常**" in line:
            errors += 1

    if not args.dry_run:
        unfin = get_unfinished_orders(_account_obj.account_id) or []
        print(f"未完成委托 {len(unfin)} 笔")

    if errors:
        print(f"\n{errors} 笔异常，需要人工看日志", flush=True)
        return 2
    return 0


# ---- 成交回报 ----
def cmd_pull_fills(args) -> int:
    acc = connect()
    from gmtrade.api import get_execution_reports
    rpts = get_execution_reports(acc.account_id) or []

    # gmtrade 的成交回报是 protobuf，字段名按实际返回为准，防御式取值
    rows = []
    for r in rpts:
        sym = from_gm_symbol(str(_g(r, "symbol", "security_id", default="")))
        side_raw = str(_g(r, "side", "ord_side", "order_side", default="")).lower()
        if "buy" in side_raw or side_raw in ("1", "200"):
            side = "buy"
        elif "sell" in side_raw or side_raw in ("2", "201"):
            side = "sell"
        else:
            pe = str(_g(r, "position_effect", default="")).lower()
            side = "buy" if "open" in pe else "sell"
        vol = int(_num(r, "volume", "last_shares", "exec_volume"))
        px = _num(r, "price", "last_px", "exec_price")
        fee = _num(r, "fee", "commission", "commission_fee")
        if vol <= 0:
            continue
        rows.append((sym, side, vol, f"{px:.4f}", f"{fee:.2f}"))

    # 同 (symbol, side, price) 聚合：reconcile 的指纹按 (symbol, side, 股数) 算，
    # 聚合不影响判定，但文件更干净、与券商"交割单"粒度更接近
    agg: dict[tuple, list] = {}
    for sym, side, vol, px, fee in rows:
        k = (sym, side, px)
        a = agg.setdefault(k, [0, 0.0])
        a[0] += vol
        a[1] += float(fee)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "side", "shares", "price", "fee"])
        for (sym, side, px), (vol, fee) in sorted(agg.items()):
            w.writerow([sym, side, vol, px, f"{fee:.2f}"])

    print(f"成交 {len(rpts) if rpts else 0} 条回报 -> 聚合 {len(agg)} 行 -> {out}")
    if not agg:
        print("[warn] 今天没有成交（空回报），reconcile 会按空批次处理")
    return 0


# ---- 紧急撤单 ----
def cmd_cancel_all(args) -> int:
    acc = connect()
    from gmtrade.api import get_unfinished_orders, order_cancel_all
    unfin = get_unfinished_orders(acc.account_id) or []
    print(f"未完成委托 {len(unfin)} 笔")
    if args.dry_run:
        print("[dry-run] 不撤")
        return 0
    ret = order_cancel_all(acc.account_id)
    print(f"撤单结果：{ret}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="掘金仿真交易桥接（gmtrade）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="看资金与持仓")

    p = sub.add_parser("pull-holdings", help="导出当前持仓为 holdings.csv")
    p.add_argument("--out", default="live/holdings.csv")
    p.add_argument("--cash-out", default=None, help="把可用现金另存一个文本文件")

    p = sub.add_parser("place-orders", help="按 orders.csv 下单")
    p.add_argument("--orders", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--order-type", choices=["limit", "market"], default="limit",
                   help="limit=按委托价挂单（默认）；market=市价")
    p.add_argument("--sells-first", action="store_true", default=True,
                   help="先卖后买，腾出资金（默认开）")
    p.add_argument("--no-sells-first", dest="sells_first", action="store_false")
    p.add_argument("--max-orders", type=int, default=200,
                   help="超过此数量拒绝下单（防呆，确认后可调大）")

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
