"""引擎启动阶段的守卫测试：数据校验、涨跌停列、暖机期。

这些测试锁定的都是"静默失效"类缺陷——配置或数据有问题时必须显式暴露，
而不是让回测悄悄跑出一个看起来正常的错误结果。
"""
import pandas as pd
import pytest

from a_share_quant.backtest import BacktestConfig, BacktestEngine
from a_share_quant.backtest.costs import CostModel
from a_share_quant.data.validation import DataValidationError
from a_share_quant.portfolio import PortfolioRules
from a_share_quant.strategies import EqualWeightStrategy, Strategy, TargetPortfolio
from a_share_quant.universe import UniverseBuilder, UniverseRules

from helpers import make_bars, make_provider


def _engine(provider, strategy=None, **kw):
    return BacktestEngine(
        provider=provider,
        strategy=strategy or EqualWeightStrategy(max_positions=2),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=2, max_weight_per_stock=0.5),
        cost_model=kw.pop("cost_model", CostModel(slippage_rate=0.0)),
        config=BacktestConfig(start_date=kw.pop("start", "2021-01-04"),
                              end_date=kw.pop("end", "2021-01-15"),
                              rebalance_frequency=kw.pop("freq", "W")),
        **kw)


# ---------- 涨跌停列缺失 ----------

def test_missing_limit_columns_raises_instead_of_silently_ignoring():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"]).drop(columns=["limit_up", "limit_down"])
    provider = make_provider(dates, ["AAA"], bars=bars)
    with pytest.raises(DataValidationError, match="limit_up"):
        _engine(provider).run()


def test_all_nan_limit_columns_raises():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"])
    bars["limit_up"] = float("nan")
    bars["limit_down"] = float("nan")
    provider = make_provider(dates, ["AAA"], bars=bars)
    with pytest.raises(DataValidationError, match="limit_up"):
        _engine(provider).run()


def test_enforce_limit_false_allows_missing_limit_columns():
    dates = pd.bdate_range("2021-01-04", periods=6)
    bars = make_bars(dates, ["AAA"]).drop(columns=["limit_up", "limit_down"])
    provider = make_provider(dates, ["AAA"], bars=bars)
    result = _engine(provider, enforce_limit=False).run()
    assert len(result.equity_curve) == len(dates)


# ---------- 引擎自带数据校验 ----------

def test_engine_runs_validation_without_script():
    """编程式调用（不经过 scripts/）也必须触发幸存者偏差哨兵。

    历史缺陷：validate_dataset 只在 run_baseline_backtest.py 里调用，
    QUICKSTART 教的编程式用法完全绕过数据校验。
    """
    dates = pd.bdate_range("2021-01-04", periods=200)
    rows = []
    for d in dates[:60]:
        rows.append({"trade_date": d, "symbol": "BBB", "open": 10.0, "high": 10.5,
                     "low": 9.5, "close": 10.0, "volume": 1e6, "amount": 1e7,
                     "adj_close": 10.0, "limit_up": 11.0, "limit_down": 9.0})
    for sym in ("AAA",):
        for d in dates:
            rows.append({"trade_date": d, "symbol": sym, "open": 10.0, "high": 10.5,
                         "low": 9.5, "close": 10.0, "volume": 1e6, "amount": 1e7,
                         "adj_close": 10.0, "limit_up": 11.0, "limit_down": 9.0})
    bars = pd.DataFrame(rows)
    instruments = pd.DataFrame([
        {"symbol": s, "name": s, "list_date": dates[0] - pd.Timedelta(days=365),
         "delist_date": pd.NaT} for s in ("AAA", "BBB")])
    provider = make_provider(dates, ["AAA", "BBB"], bars=bars, instruments=instruments)
    result = _engine(provider, freq="M", end=str(dates[-1].date())).run()
    assert any("survivorship" in w for w in result.validation_warnings)


def test_validation_can_be_disabled_explicitly():
    dates = pd.bdate_range("2021-01-04", periods=6)
    provider = make_provider(dates, ["AAA"])
    result = _engine(provider, validate=False).run()
    # validate=False 关的是数据校验；股票池的运行期诊断（例如未配板块白名单）
    # 仍会保留，因此这里只断言"数据校验类"告警不再出现。
    assert not any("survivorship" in w or "NaN" in w
                   for w in result.validation_warnings)


# ---------- 暖机期 ----------

class _SlowStrategy(Strategy):
    name = "slow"
    warmup = 100

    def generate_target(self, as_of, bars, universe):
        return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights={})


def test_warmup_longer_than_window_warns():
    dates = pd.bdate_range("2021-01-04", periods=15)
    provider = make_provider(dates, ["AAA"])
    result = _engine(provider, strategy=_SlowStrategy(),
                     end=str(dates[-1].date())).run()
    assert any(w.startswith("warmup:") for w in result.validation_warnings)


def test_warmup_within_window_is_silent():
    dates = pd.bdate_range("2021-01-04", periods=15)
    provider = make_provider(dates, ["AAA"])

    class _Ok(Strategy):
        name = "ok"
        warmup = 3

        def generate_target(self, as_of, bars, universe):
            return TargetPortfolio(as_of=pd.Timestamp(as_of).normalize(), weights={})

    result = _engine(provider, strategy=_Ok(), end=str(dates[-1].date())).run()
    assert not any(w.startswith("warmup:") for w in result.validation_warnings)


# ---------- 基准 ----------

def test_benchmark_without_adj_close_falls_back_to_close():
    """bars 无 adj_close 列时加载基准不应抛 KeyError（旧实现的缺陷）。"""
    dates = pd.bdate_range("2021-01-04", periods=8)
    bars = make_bars(dates, ["AAA", "000300"]).drop(columns=["adj_close"])
    provider = make_provider(dates, ["AAA", "000300"], bars=bars)
    engine = BacktestEngine(
        provider=provider,
        strategy=EqualWeightStrategy(max_positions=1),
        universe_builder=UniverseBuilder(provider, UniverseRules(min_list_days=0)),
        portfolio_rules=PortfolioRules(max_positions=1, max_weight_per_stock=1.0),
        cost_model=CostModel(slippage_rate=0.0),
        config=BacktestConfig(start_date=str(dates[0].date()),
                              end_date=str(dates[-1].date()),
                              rebalance_frequency="W", benchmark="000300"))
    result = engine.run()
    assert result.benchmark is not None
    assert result.benchmark.name == "000300"


# ---------------------------------------------------------------- R08 F-44
def test_momentum_top_n_follows_max_positions():
    """R08 F-44：动量的 `top_n` 必须跟 `--max-positions` 走，且传入 liquidity_pct / capacity_aum。

    原来写死配置里的 50，于是 `--max-positions 10` 时动量仍选 50 只，再由
    `build_target_weights` 按「权重降序、同权重字典序」截到 10 只。动量是**等权**，
    所以截断完全按代码字典序——名次信息在截断那一步丢光（实测拿到 000000~000009）。

    低波分支一直用的是 `top_n=max_pos`，只有动量分支是例外，这里防它退回去。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_baseline_backtest.py").read_text(encoding="utf-8")

    i = src.index('elif args.strategy == "momentum":')
    seg = src[i:src.index("else:", i)]
    assert "top_n=max_pos" in seg, "动量 top_n 必须跟 max_pos"
    assert "liquidity_pct=args.liquidity" in seg, "动量必须接入 liquidity_pct"
    assert "capacity_aum=args.capacity_aum" in seg, "动量必须接入 capacity_aum"
    assert 'top_n=pf.get("max_positions"' not in seg, \
        "不能再写死配置里的 max_positions"



# ---------------------------------------------------------------- gm_bridge 符号换算
def test_gm_symbol_conversion_covers_all_three_exchanges():
    """R09：下单方向对不上下错市场就是真金白银的事故，换算必须锁住。

    内部用 6 位纯代码（`000001`），掘金用 `SZSE.000001`。
    6/9 开头沪市、0/1/2/3 深市、4/8 北交所。
    """
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "scripts" / "gm_bridge.py"
    spec = importlib.util.spec_from_file_location("gm_bridge", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cases = [
        ("600000", "SHSE.600000"),   # 沪主板
        ("688521", "SHSE.688521"),   # 科创板
        ("900901", "SHSE.900901"),   # 沪 B
        ("000001", "SZSE.000001"),   # 深主板
        ("300561", "SZSE.300561"),   # 创业板
        ("002258", "SZSE.002258"),   # 深主板小板
        ("430047", "BJSE.430047"),   # 北交所
        ("830799", "BJSE.830799"),
    ]
    for code, expected in cases:
        assert mod.to_gm_symbol(code) == expected, f"{code} -> {mod.to_gm_symbol(code)}"
        assert mod.from_gm_symbol(expected) == code

    # 已带前缀的原样返回
    assert mod.to_gm_symbol("SHSE.600000") == "SHSE.600000"
    # 非法输入要报错，不能猜
    for bad in ("60000", "abc", "6000001", ""):
        try:
            mod.to_gm_symbol(bad)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝 {bad!r}")


# ---------------------------------------------------------------- 钉钉推送
def test_dingtalk_sign_algorithm_matches_official_spec():
    """钉钉加签：HMAC-SHA256(secret, "{ts}\n{secret}") -> base64 -> urlencode。

    算法错了消息就发不出去，而且报错是「签名不匹配」，很不直观。
    """
    import base64, hashlib, hmac, urllib.parse
    secret = "SECxxxx"
    ts = "1792810000000"
    string_to_sign = f"{ts}\n{secret}"
    code = hmac.new(secret.encode(), string_to_sign.encode(),
                    digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(code))
    # 复算一次必须一致
    code2 = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(),
                     digestmod=hashlib.sha256).digest()
    assert base64.b64encode(code) == base64.b64encode(code2)
    # urlencode 后不能有裸的 + / = 等
    assert all(ch.isalnum() or ch in "-_.%~" for ch in sign), sign
    # string_to_sign 必须是 "ts\nsecret"，不是 "secret\nts"
    assert hmac.new(secret.encode(), f"{secret}\n{ts}".encode(),
                    digestmod=hashlib.sha256).digest() != code, \
        "顺序错了：必须是 timestamp 在前"


def test_notify_config_is_optional_and_non_blocking():
    """没有 config/notify.yaml 时推送静默跳过，绝不让主流程崩。"""
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "scripts" / "live_auto.py"
    spec = importlib.util.spec_from_file_location("live_auto", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 缺配置 → 返回 False，不抛异常
    saved = mod._load_notify
    mod._load_notify = lambda: {}
    try:
        assert mod.dingtalk_push("t", "x") is False
    finally:
        mod._load_notify = saved


# ---------------------------------------------------------------- 单一解释器（gmtrade 已移除）
def test_gm_bridge_does_not_import_gmtrade():
    """交易只走 gm SDK 策略模式。`gmtrade` 在 3.24.2.1 终端上已不可用，
    代码里再出现它只有一个结果：静默失败或挂死，所以直接从入口层禁止。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "gm_bridge.py").read_text(encoding="utf-8")
    assert "import gmtrade" not in src
    assert "from gmtrade" not in src


def test_live_yaml_has_single_interpreter_without_python_trade():
    """分离的"交易解释器"是 gmtrade 时代的产物，已随 gmtrade 一起移除。

    两个 key 指向同一路径是纯负担：谁把 python_trade 指回 3.10，gm 就 import 不到，
    而报错埋在子进程 stderr 里，排查成本极高。
    """
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "config" / "live.yaml").read_text(encoding="utf-8")
    assert "python_trade" not in text
    assert "python_data" in text


def test_config_drops_legacy_python_trade_with_warning(tmp_path, monkeypatch, capsys):
    """旧配置文件里的 python_trade 被丢弃并告警，不能静默继续。"""
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "scripts" / "live_auto.py"
    spec = importlib.util.spec_from_file_location("live_auto_cfg", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "live.yaml").write_text(
        "python_data:  " + r"C:\py312\python.exe" + "\n"
        "python_trade: " + r"C:\py310\python.exe" + "\n"
        "position: 0.5\n",
        encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    cfg = mod.config()
    assert "python_trade" not in cfg
    assert cfg["python_data"] == r"C:\py312\python.exe"
    assert "python_trade" in capsys.readouterr().out, "必须告警，不能静默丢弃"


# ---------------------------------------------------------------- 执行日涨跌停闸门
def _load_script(name: str):
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", "_t"), p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_execution_gate_matches_backtest_semantics():
    """下单层闸门与 backtest/execution.py 逐条同口径（2026-10-27 修）。

    旧口径在信号层用 t 日收盘对 t+1 开盘做近似，hype_watch 的候选恰是涨停股，
    实测 3/3 被 blocked:limit_up，策略永远买不进。判定挪到下单时：
    用执行日现价（盘前 = 集合竞价参考价）对执行日官方涨跌停价。

    口径（execution.py 买卖都拦触板，**不看是否持有**）：
      买 价 >= 涨停价            -> blocked:limit_up
      卖 价 <= 跌停价            -> blocked:limit_down
      停牌                      -> blocked:suspended
      缺行情                    -> blocked:no_quote
      涨跌停价缺失/非正          -> blocked:no_limit_data
      未持有的跌停股可以买       （卖难买易，execution.py 只拦卖）
    """
    mod = _load_script("gm_bridge.py")
    quotes = {
        "AAA": {"price": 11.0, "limit_up": 11.0, "limit_down": 9.0, "suspended": False},  # 涨停
        "BBB": {"price": 9.0, "limit_up": 11.0, "limit_down": 9.0, "suspended": False},   # 跌停
        "CCC": {"price": 10.0, "limit_up": 11.0, "limit_down": 9.0, "suspended": False},  # 正常
        "DDD": {"price": 10.0, "limit_up": 11.0, "limit_down": 9.0, "suspended": True},   # 停牌
        "EEE": {"price": None, "limit_up": 11.0, "limit_down": 9.0, "suspended": False},  # 无行情
        "FFF": {"price": 10.0, "limit_up": 0.0, "limit_down": 0.0, "suspended": False},   # 无涨跌停价
    }

    def od(sym, side, shares=100):
        return {"symbol": sym, "side": side, "shares": shares, "price": 10.0,
                "amount": 1000.0, "note": "rebalance"}

    orders = [od("AAA", "buy"), od("AAA", "buy", 50), od("BBB", "sell", 200),
              od("BBB", "buy"), od("CCC", "buy"), od("CCC", "sell"),
              od("DDD", "sell", 300), od("EEE", "buy"), od("FFF", "buy"),
              od("GGG", "buy"),                                   # 完全没有行情
              {"symbol": "HHH", "side": "hold", "shares": 0,
               "price": 10.0, "amount": 0.0, "note": "blocked:old"}]   # hold 行透传
    out = mod.apply_execution_gate(orders, quotes)
    by = {}
    for r in out:
        by[(r["symbol"], r.get("note", ""))] = r

    lu = [r for r in out if r["symbol"] == "AAA"]
    assert all(r["side"] == "hold" and r["note"] == "blocked:limit_up" for r in lu), \
        "涨停不追买——且不看是否持有（execution.py 也不看）"
    sell_bbb = [r for r in out if r["symbol"] == "BBB" and r["shares"] in (0, 200)][0]
    assert sell_bbb["side"] == "hold" and sell_bbb["note"] == "blocked:limit_down"
    buy_bbb = [r for r in out if r["symbol"] == "BBB" and r["shares"] == 100][0]
    assert buy_bbb["side"] == "buy", "未持有的跌停股可以买（卖难买易）"
    assert by[("CCC", "rebalance")]["side"] == "buy" or any(
        r["symbol"] == "CCC" and r["side"] == "sell" for r in out), "正常股不受影响"
    ddd = [r for r in out if r["symbol"] == "DDD"][0]
    assert ddd["note"] == "blocked:suspended"
    assert by[("EEE", "blocked:no_quote")]["side"] == "hold"
    assert by[("FFF", "blocked:no_limit_data")]["side"] == "hold"
    assert by[("GGG", "blocked:no_quote")]["side"] == "hold"
    assert by[("HHH", "blocked:old")]["side"] == "hold", "hold 行原样透传，不重复加工"
    # 被拦下的行必须把股数/金额清零，否则对账会当成"该成交而没成交"
    for r in out:
        if str(r.get("note", "")).startswith("blocked:"):
            assert r["shares"] == 0 and float(r["amount"]) == 0.0


def test_orders_for_reconcile_prefers_fresh_placement(tmp_path):
    """对账必须用「实际发送」的 placement.csv，否则闸门拦下的单会被算成未成交，
    8 周要测的成交率直接被污染。"""
    mod = _load_script("live_auto.py")
    orders = tmp_path / "2026-10-27" / "orders.csv"
    orders.parent.mkdir(parents=True)
    orders.write_text("symbol,side\nAAA,buy\n", encoding="utf-8")
    assert mod.orders_for_reconcile(orders) == orders, "没有 placement.csv 时退回 orders.csv"

    placement = orders.parent / "placement.csv"
    placement.write_text("symbol,side\nAAA,buy\n", encoding="utf-8")
    import os
    os.utime(placement, (orders.stat().st_mtime + 5, orders.stat().st_mtime + 5))
    assert mod.orders_for_reconcile(orders) == placement, "新的 placement.csv 优先"

    # placement 比 orders 旧 = 属于上一轮，不能用
    os.utime(placement, (orders.stat().st_mtime - 500, orders.stat().st_mtime - 500))
    assert mod.orders_for_reconcile(orders) == orders


# ---------------------------------------------------------------- 独立看门狗
def test_watchdog_flags_missing_and_failed_runs_and_dedupes():
    """看门狗存在的理由：进程没起来时（2026-10-26/27 计划任务路径错、
    Python 退出码 2），进程内推送是沉默的——必须从外面按时刻表核对心跳。

    判定规则：到点+宽限仍无心跳 -> missing；有心跳但 rc!=0 -> failed；
    dry_run 心跳不算数；同类告警只报一次；周末不判；16:00 后发当日汇总。
    """
    import datetime as dt
    mod = _load_script("watchdog.py")
    wed = dt.datetime(2026, 10, 28, 9, 5)      # 周三，data 宽限已过(09:00)

    # 1) 没心跳 -> data missing；trade/settle 未到点不判
    alerts, alerted, sent = mod.check(wed, {}, {}, set())
    assert [a["stage"] for a in alerts] == ["data"] and alerts[0]["kind"] == "missing"

    # 2) 同类不重复报
    alerts2, alerted2, _ = mod.check(wed, {}, alerted, sent)
    assert alerts2 == [], "同一 (日, stage, kind) 只报一次"

    # 3) 演练心跳不算跑过
    hb = {"2026-10-28": {"data": {"rc": 0, "at": "x", "dry_run": True}}}
    alerts3, _, _ = mod.check(wed, hb, {}, set())
    assert alerts3[0]["kind"] == "missing", "dry-run 心跳不能把坏任务掩盖掉"

    # 4) 跑了但失败 -> failed（进程内推送的兜底第二只眼）
    hb = {"2026-10-28": {"data": {"rc": 2, "at": "x", "dry_run": False}}}
    alerts4, _, _ = mod.check(wed, hb, {}, set())
    assert alerts4[0]["kind"] == "failed" and "rc=2" in alerts4[0]["text"]

    # 5) rc=0 正常 -> 不报
    hb = {"2026-10-28": {"data": {"rc": 0, "at": "x", "dry_run": False}}}
    alerts5, _, _ = mod.check(wed, hb, {}, set())
    assert alerts5 == []

    # 6) 周末不判
    sat = dt.datetime(2026, 10, 31, 10, 0)
    alerts6, _, _ = mod.check(sat, {}, {}, set())
    assert alerts6 == []

    # 7) 16:00 后发当日汇总（看门狗自身的存活证明），且只发一次
    eve = dt.datetime(2026, 10, 28, 16, 5)
    hb = {"2026-10-28": {s: {"rc": 0, "at": "09:00", "dry_run": False}
                         for s in mod.SCHEDULE}}
    alerts7, _, sent7 = mod.check(eve, hb, {}, set())
    kinds = [a["kind"] for a in alerts7]
    assert kinds == ["summary"], f"当日全正常时只应有汇总，实际 {kinds}"
    alerts8, _, sent8 = mod.check(eve, hb, {}, sent7)
    assert not any(a["kind"] == "summary" for a in alerts8), "汇总一天只发一次"


def test_write_heartbeat_isolates_dry_run(tmp_path, monkeypatch):
    """演练心跳写到 live/_dryrun，不能碰生产心跳（2026-10-28 实测的漏）。

    外部看门狗只看文件 mtime，分不出演练与真跑。前一晚一次演练把心跳续到 22:37，
    本该 09:35 触发的「今天整天没跑」要拖到 16:37 ——已过监控时段，兜底失效。
    """
    import json
    mod = _load_script("live_auto.py")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    cfg = {"out_dir": "live"}
    mod.write_heartbeat(cfg, [("data", 0)], dry_run=True)
    mod.write_heartbeat(cfg, [("trade", 2)], dry_run=False)
    day = mod.today()

    prod = json.loads((tmp_path / "live" / "heartbeat.json").read_text(encoding="utf-8"))
    assert "data" not in prod[day], "演练不能写生产心跳"
    assert prod[day]["trade"]["dry_run"] is False and prod[day]["trade"]["rc"] == 2

    drill = json.loads((tmp_path / "live" / "_dryrun" / "heartbeat.json")
                       .read_text(encoding="utf-8"))
    assert drill[day]["data"]["dry_run"] is True, "演练记录自己留着，便于排查"


# ---------------------------------------------------------------- 查询命令有数据时（评审 P0-1 回归）
_FAKE_GM_API = '''
"""假 gm SDK：测试专用。有持仓/有成交，逼出生成代码里的 NameError 类缺陷。"""
MODE_LIVE = 1
OrderSide_Buy, OrderSide_Sell = 1, 2
OrderType_Limit, OrderType_Market = 1, 2
PositionEffect_Open, PositionEffect_Close = 1, 2
OrderDuration_GFD = 3

def set_account_id(a): pass

class Cash:
    nav = 1000000.0
    available = 1000000.0
def get_cash(): return Cash()

class Pos:
    symbol = "SZSE.000001"; volume = 100; cost_price = 10.0; price = 11.0
def get_position(): return [Pos()]

class ExecBuy:
    symbol = "SZSE.000001"; side = 1; volume = 100; price = 10.5; fee = 5.0
class ExecSell:
    symbol = "SHSE.600000"; side = 2; volume = 200; price = 8.0; fee = 3.0
def get_execution_reports(): return [ExecBuy(), ExecSell()]

class _FakeDF:
    def __init__(self, rows): self._rows = rows
    def __len__(self): return len(self._rows)
    def to_dict(self, orient=None): return self._rows

def get_history_instruments(symbols=None, start_date=None, end_date=None,
                            fields=None, df=False):
    return _FakeDF([{"symbol": s, "upper_limit": 99.0, "lower_limit": 1.0,
                     "is_suspended": False} for s in (symbols or [])])

def current(syms):
    return [{"symbol": s, "price": 50.0, "open": 50.0} for s in (syms or [])]

def order_volume(**kw): return "ORDER1"
def order_cancel_all(): return 0
def stop(): pass

def run(filename=None, mode=None, token=None):
    import __main__
    __main__.init(None)
'''


@pytest.fixture()
def fake_gm():
    """把假 gm 包放到 live/_tmp/gm：子进程脚本所在目录是 sys.path[0]，import gm 命中它。"""
    import shutil
    from pathlib import Path
    gm_dir = Path(__file__).resolve().parents[1] / "live" / "_tmp" / "gm"
    gm_dir.mkdir(parents=True, exist_ok=True)
    (gm_dir / "__init__.py").write_text("", encoding="utf-8")
    (gm_dir / "api.py").write_text(_FAKE_GM_API, encoding="utf-8")
    yield gm_dir
    shutil.rmtree(gm_dir, ignore_errors=True)


def test_status_and_pull_holdings_with_positions(fake_gm, monkeypatch, capsys, tmp_path):
    """有持仓时 status / pull-holdings 必须 rc=0 且数值正确（评审 P0-1 回归锁）。

    旧缺陷：生成的子进程模块里引用了 gm_bridge 自己的 from_gm_symbol——
    空仓时循环体不执行看着全绿，有持仓必 NameError，实盘链路直接断。
    """
    import argparse
    mod = _load_script("gm_bridge.py")
    monkeypatch.setattr(mod, "_load_account", lambda: ("faketok", "fakeacc"))

    rc = mod.cmd_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "1,000,000" in out, f"nav 应可读，实际输出 {out!r}"
    assert "000001" in out and "SZSE." not in out, f"符号应剥掉交易所前缀：{out!r}"

    h = tmp_path / "h.csv"
    rc = mod.cmd_pull_holdings(argparse.Namespace(out=str(h),
                                                  cash_out=str(tmp_path / "c.txt")))
    assert rc == 0
    text = h.read_text(encoding="utf-8")
    assert "000001" in text and "100" in text, f"持仓行应含 000001,100：{text!r}"
    assert "SZSE." not in text


def test_pull_fills_directions_when_there_are_fills(fake_gm, monkeypatch, tmp_path):
    """有成交时 pull-fills 必须 rc=0 且买卖方向判定正确（评审 P0-1 回归锁）。

    同一缺陷的第三处只在有成交时才执行到；方向判错会让对账把买卖记反。
    """
    import argparse
    mod = _load_script("gm_bridge.py")
    monkeypatch.setattr(mod, "_load_account", lambda: ("faketok", "fakeacc"))

    f = tmp_path / "f.csv"
    rc = mod.cmd_pull_fills(argparse.Namespace(out=str(f)))
    assert rc == 0
    rows = [ln.split(",") for ln in f.read_text(encoding="utf-8").strip().splitlines()[1:]]
    by = {r[0]: r for r in rows}
    assert by["000001"][1] == "buy", f"side=1 应判 buy：{rows}"
    assert by["600000"][1] == "sell", f"side=2 应判 sell：{rows}"
    assert by["000001"][2] == "100" and by["600000"][2] == "200"


def test_real_place_orders_is_not_retried():
    """真实下单不得包在 run_step_retry 里（评审 P0-2 回归锁）。

    cmd_place_orders 部分委托异常时返回 2，重试会把 orders.csv 整批重发，
    已成功的委托重复买入。演练（dry-run）幂等，保留 retry 无所谓。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "live_auto.py").read_text(encoding="utf-8")
    assert 'run_step_retry("place-orders"' not in src, "真实下单禁止重试（重复下单风险）"
    assert 'run_step("place-orders"' in src
    assert 'run_step_retry("place-orders(dry-run)"' in src, "演练幂等，可保留 retry"


# ---------------------------------------------------------------- 闸门全拦的退出码（评审 P1-A）
def test_gate_blocks_all_on_missing_data_exits_nonzero(tmp_path, monkeypatch, capsys):
    """涨跌停价没取到时"全部拦下"不能当成功。

    旧行为 return 0：stage_trade 以为下单成功、钉钉推 ✅，实际一笔没发。
    数据缺口必须交人工（退出码 2）。
    """
    import argparse
    mod = _load_script("gm_bridge.py")
    monkeypatch.setattr(mod, "_load_account", lambda: ("tok", "acc"))
    monkeypatch.setattr(mod, "_fetch_execution_quotes", lambda codes, date: {})
    orders = tmp_path / "orders.csv"
    orders.write_text("symbol,side,shares,price,amount,note\n"
                      "600000,buy,100,10.00,1000,x\n", encoding="utf-8")
    rc = mod.cmd_place_orders(argparse.Namespace(
        orders=str(orders), placement=None, dry_run=False,
        order_type="limit", sells_first=True, max_orders=200))
    out = capsys.readouterr().out
    assert rc == 2, f"数据缺失导致全拦必须退出码 2，实际 {rc}；输出 {out!r}"
    assert "数据没取到" in out
    assert not (tmp_path / "placement.csv").exists(), "一笔没发就不能写 placement.csv"


def test_gate_blocks_all_on_limit_up_exits_zero(tmp_path, monkeypatch, capsys):
    """全部涨停买不进是正常业务结果，退出码仍是 0（否则每个一字板日都告警）。"""
    import argparse
    mod = _load_script("gm_bridge.py")
    monkeypatch.setattr(mod, "_load_account", lambda: ("tok", "acc"))
    quotes = {"600000": {"price": 11.0, "limit_up": 11.0,
                         "limit_down": 9.0, "suspended": False}}
    monkeypatch.setattr(mod, "_fetch_execution_quotes", lambda codes, date: quotes)
    orders = tmp_path / "orders.csv"
    orders.write_text("symbol,side,shares,price,amount,note\n"
                      "600000,buy,100,11.00,1100,x\n", encoding="utf-8")
    rc = mod.cmd_place_orders(argparse.Namespace(
        orders=str(orders), placement=None, dry_run=False,
        order_type="limit", sells_first=True, max_orders=200))
    out = capsys.readouterr().out
    assert rc == 0, f"触板属正常业务，应 0；实际 {rc}；输出 {out!r}"
    assert "正常业务" in out


# ---------------------------------------------------------------- 看门狗认交易日历（评审 P1-C）
def test_watchdog_skips_exchange_holiday():
    """日历范围内的休市日不巡检。2026-10-02 是国庆假期（周五），旧代码报假警。"""
    import datetime as dt
    mod = _load_script("watchdog.py")
    cal = ({"2026-09-30", "2026-10-08", "2026-10-09"}, "2026-10-09")
    alerts, _, _ = mod.check(dt.datetime(2026, 10, 2, 10, 0), {}, {}, set(), cal)
    assert alerts == [], f"国庆假期不该告警，实际 {alerts}"


def test_watchdog_still_alerts_when_calendar_is_stale():
    """日历没覆盖今天时必须照常巡检。

    否则数据一停更（正是最该报警的时候），看门狗反而对所有工作日闭嘴。
    """
    import datetime as dt
    mod = _load_script("watchdog.py")
    cal = ({"2026-09-30", "2026-10-08"}, "2026-10-08")
    alerts, _, _ = mod.check(dt.datetime(2026, 10, 27, 10, 0), {}, {}, set(), cal)
    assert [a["kind"] for a in alerts] == ["missing", "missing"], \
        f"日历只到 10-08，10-27 必须照常告警，实际 {alerts}"


def test_watchdog_without_calendar_keeps_old_behavior():
    """读不到日历时退回只按周末判（宁可误报不可漏报）。"""
    import datetime as dt
    mod = _load_script("watchdog.py")
    alerts, _, _ = mod.check(dt.datetime(2026, 10, 27, 10, 0), {}, {}, set(), None)
    assert len(alerts) == 2
    alerts, _, _ = mod.check(dt.datetime(2026, 10, 31, 10, 0), {}, {}, set(), None)
    assert alerts == []


def test_trade_day_ok_treats_beyond_calendar_as_possible():
    """超出日历范围 ≠ 休市，而是"日历还没更新"，必须放行。"""
    mod = _load_script("watchdog.py")
    cal = ({"2026-10-08"}, "2026-10-08")
    assert mod._trade_day_ok("2026-10-08", cal) is True
    assert mod._trade_day_ok("2026-10-05", cal) is False     # 日历内但没有该日 = 休市
    assert mod._trade_day_ok("2026-10-27", cal) is True      # 超出日历范围
    assert mod._trade_day_ok("2026-10-27", None) is True


# ---------------------------------------------------------------- 部分数据缺口不能推 ✅（评审 P2-4）
def _two_orders(tmp_path):
    o = tmp_path / "orders.csv"
    o.write_text("symbol,side,shares,price,amount,note\n"
                 "600000,buy,100,10.00,1000,x\n"
                 "000001,buy,200,12.00,2400,y\n", encoding="utf-8")
    return o


def _partial_gap_quotes(codes, date):
    """600000 正常放行；000001 只有价格、没有涨跌停价 → blocked:no_limit_data。"""
    return {"600000": {"price": 10.0, "limit_up": 11.0, "limit_down": 9.0,
                       "suspended": False},
            "000001": {"price": 12.0, "limit_up": 0.0, "limit_down": 0.0,
                       "suspended": False}}


def test_gate_partial_data_gap_exits_nonzero_but_still_sends(tmp_path, monkeypatch, capsys):
    """一笔放行、一笔数据缺失时也必须退出码 2（评审 P2-4）。

    8ba876e 只修了「全部拦下」，部分缺口时 sent 非空 → return 0 → stage_trade
    当成功、钉钉推 ✅，那只根本没拿到涨跌停价的票就此消失，没人会去看行情源。
    但已放行的单必须照常发送并写 placement.csv——不能因为一笔缺数据就全盘不发。
    """
    import argparse
    mod = _load_script("gm_bridge.py")
    monkeypatch.setattr(mod, "_load_account", lambda: ("tok", "acc"))
    monkeypatch.setattr(mod, "_fetch_execution_quotes", _partial_gap_quotes)
    sent_bodies = []
    monkeypatch.setattr(mod, "_run_in_context",
                        lambda body, timeout=120: (sent_bodies.append(body),
                                                   {"ok": True, "orders": []})[1])

    orders = _two_orders(tmp_path)
    rc = mod.cmd_place_orders(argparse.Namespace(
        orders=str(orders), placement=None, dry_run=False,
        order_type="limit", sells_first=True, max_orders=200))
    out = capsys.readouterr().out

    assert rc == 2, f"部分数据缺口必须交人工，实际 rc={rc}；输出 {out!r}"
    assert "no_limit_data 1" in out, f"要指出缺口类型与笔数：{out!r}"
    assert "已发出 1 笔" in out, f"要说明放行的单已发出、照常对账：{out!r}"
    placement = tmp_path / "placement.csv"
    assert placement.exists(), "放行的单照常写 placement.csv（对账口径）"
    assert "600000" in placement.read_text(encoding="utf-8")
    assert "000001" not in placement.read_text(encoding="utf-8"), \
        "被拦下的票不能进 placement.csv，否则对账会算成未成交"
    skipped = (tmp_path / "placement_skipped.csv").read_text(encoding="utf-8")
    assert "000001" in skipped and "blocked:no_limit_data" in skipped
    assert sent_bodies and "SHSE.600000" in sent_bodies[0]


def test_gate_dry_run_reports_gap_but_does_not_fail(tmp_path, monkeypatch, capsys):
    """演练遇到数据缺口要能看见，但不退出非零（演练不发单，失败也没意义）。"""
    import argparse
    mod = _load_script("gm_bridge.py")
    monkeypatch.setattr(mod, "_load_account", lambda: ("tok", "acc"))
    monkeypatch.setattr(mod, "_fetch_execution_quotes", _partial_gap_quotes)
    orders = _two_orders(tmp_path)
    rc = mod.cmd_place_orders(argparse.Namespace(
        orders=str(orders), placement=None, dry_run=True,
        order_type="limit", sells_first=True, max_orders=200))
    out = capsys.readouterr().out
    assert rc == 0, "演练不阻断"
    assert "1 笔因行情/涨跌停价缺失被拦下" in out, f"演练也要看得见缺口：{out!r}"
    assert not (tmp_path / "placement.csv").exists()


def test_skipped_summary_surfaces_gap_in_dingtalk(tmp_path, monkeypatch):
    """钉钉文案要能看出「拦下几笔、其中几笔是数据缺失」（评审 P2-4）。

    退出码只说"有问题"，不说"少了哪几笔"；数据缺口和封板买不进是两回事，
    前者要人去查行情源，必须在推送里单独点出来。
    """
    mod = _load_script("live_auto.py")
    day = tmp_path / "live" / "2026-10-27"
    day.mkdir(parents=True)
    (day / "orders.csv").write_text("symbol,side\n", encoding="utf-8")
    (day / "placement_skipped.csv").write_text(
        "symbol,side,shares,price,amount,note\n"
        "600519,buy,0,10.00,0,blocked:limit_up\n"
        "000001,buy,0,12.00,0,blocked:no_limit_data\n", encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    text = mod.skipped_summary({"out_dir": "live"})
    assert "闸门拦下" in text and "2 笔" in text, text
    assert "数据缺失 1" in text, f"缺口必须单独点出：{text}"
    assert "触板/停牌 1" in text

    # 全是触板时不提"数据缺失"（一字板日不该喊人查行情源）
    (day / "placement_skipped.csv").write_text(
        "symbol,side,shares,price,amount,note\n"
        "600519,buy,0,10.00,0,blocked:limit_up\n", encoding="utf-8")
    text = mod.skipped_summary({"out_dir": "live"})
    assert "数据缺失" not in text and "触板/停牌 1" in text, text

    # 没有 skipped 文件（只算不下 / dry-run）就不加这行
    (day / "placement_skipped.csv").unlink()
    assert mod.skipped_summary({"out_dir": "live"}) == ""


def test_data_gap_notes_stay_in_sync_across_scripts():
    """live_auto 的判别口径必须和 gm_bridge 一致（它按设计不 import 那个模块）。"""
    bridge = _load_script("gm_bridge.py")
    auto = _load_script("live_auto.py")
    assert tuple(bridge.DATA_GAP_NOTES) == tuple(auto.DATA_GAP_NOTES), \
        f"gm_bridge={bridge.DATA_GAP_NOTES} live_auto={auto.DATA_GAP_NOTES}"


# ---------------------------------------------------------------- 零股不进对账口径（2026-10-27 探针）
def test_zero_share_orders_do_not_enter_placement(tmp_path, monkeypatch, capsys):
    """0 股委托不发单，也不能进 placement.csv——它是对账口径。

    原顺序是「先写盘、再判 shares<=0」，0 股那笔照样落进 placement.csv，
    reconcile 会记成"该成交而没成交"，白白拉低成交率。实盘里
    build_plan 在资金不足时会出 0 股行，不是理论情形。
    """
    import argparse
    mod = _load_script("gm_bridge.py")
    monkeypatch.setattr(mod, "_load_account", lambda: ("tok", "acc"))
    monkeypatch.setattr(mod, "_fetch_execution_quotes", lambda codes, date: {
        "600000": {"price": 10.0, "limit_up": 11.0, "limit_down": 9.0, "suspended": False},
        "000001": {"price": 12.0, "limit_up": 13.2, "limit_down": 10.8, "suspended": False},
    })
    monkeypatch.setattr(mod, "_run_in_context",
                        lambda body, timeout=120: {"ok": True, "orders": []})
    orders = tmp_path / "orders.csv"
    orders.write_text("symbol,side,shares,price,amount,note\n"
                      "600000,buy,0,10.00,0,zero\n"
                      "000001,buy,100,12.00,1200,ok\n", encoding="utf-8")
    rc = mod.cmd_place_orders(argparse.Namespace(
        orders=str(orders), placement=None, dry_run=False,
        order_type="limit", sells_first=True, max_orders=200))
    assert rc == 0
    text = (tmp_path / "placement.csv").read_text(encoding="utf-8")
    assert "000001" in text
    assert "600000" not in text, f"0 股委托不能进对账口径：{text!r}"
    assert "shares=0" in capsys.readouterr().out


def test_all_zero_share_orders_writes_no_placement(tmp_path, monkeypatch, capsys):
    """全都 0 股时连 placement.csv 都不该写（否则对账拿到一份空的"已发送"）。"""
    import argparse
    mod = _load_script("gm_bridge.py")
    monkeypatch.setattr(mod, "_load_account", lambda: ("tok", "acc"))
    monkeypatch.setattr(mod, "_fetch_execution_quotes", lambda codes, date: {
        "600000": {"price": 10.0, "limit_up": 11.0, "limit_down": 9.0, "suspended": False}})
    monkeypatch.setattr(mod, "_run_in_context",
                        lambda body, timeout=120: {"ok": True, "orders": []})
    orders = tmp_path / "orders.csv"
    orders.write_text("symbol,side,shares,price,amount,note\n"
                      "600000,buy,0,10.00,0,zero\n", encoding="utf-8")
    rc = mod.cmd_place_orders(argparse.Namespace(
        orders=str(orders), placement=None, dry_run=False,
        order_type="limit", sells_first=True, max_orders=200))
    assert rc == 0
    assert "股数全为 0" in capsys.readouterr().out
    assert not (tmp_path / "placement.csv").exists()


# ---------------------------------------------------------------- 演练不能污染生产目录
def test_dry_run_writes_signals_to_isolated_dir(tmp_path, monkeypatch):
    """演练的委托必须落在 live/_dryrun，不能留在生产目录。

    PR #12 让 live_signal_hype 的 --dry-run 不写盘，但 live_auto 从没把 --dry-run
    传给信号脚本——演练照样写 live/<date>/orders.csv，下一轮 find_recent_orders
    会把它当成真委托发出去。修法：演练写到隔离目录，仍能过闸门，生产目录零污染。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "live_auto.py").read_text(
        encoding="utf-8")
    assert 'sig_out = "live/_dryrun" if args.dry_run else cfg["out_dir"]' in src, \
        "演练必须写到隔离目录"
    assert "find_recent_orders(cfg, out_dir=sig_out)" in src, \
        "取委托也要去隔离目录，否则会翻到生产目录的上一轮"

    mod = _load_script("live_auto.py")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    prod = tmp_path / "live" / "2026-10-27"
    prod.mkdir(parents=True)
    (prod / "orders.csv").write_text("symbol,side\n000001,buy\n", encoding="utf-8")

    assert mod.find_recent_orders({"out_dir": "live"}) == prod / "orders.csv"
    assert mod.find_recent_orders({"out_dir": "live"}, out_dir="live/_dryrun") is None, \
        "隔离目录为空时必须返回 None，不能退回去读生产目录"
