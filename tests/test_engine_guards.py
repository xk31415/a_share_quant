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
        "python_data:  C://py312//python.exe/n"
        "python_trade: C://py310//python.exe/n"
        "position: 0.5\n",
        encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    cfg = mod.config()
    assert "python_trade" not in cfg
    assert cfg["python_data"] == r"C:/py312/python.exe"
    assert "python_trade" in capsys.readouterr().out, "必须告警，不能静默丢弃"


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


def test_write_heartbeat_marks_dry_run(tmp_path, monkeypatch):
    """心跳必须带 dry_run 标记并只留 14 天，watchdog 靠它区分"跑过"和"演练过"。"""
    import json
    mod = _load_script("live_auto.py")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    cfg = {"out_dir": "live"}
    mod.write_heartbeat(cfg, [("data", 0)], dry_run=True)
    mod.write_heartbeat(cfg, [("trade", 2)], dry_run=False)
    data = json.loads((tmp_path / "live" / "heartbeat.json").read_text(encoding="utf-8"))
    day = mod.today()
    assert data[day]["data"]["dry_run"] is True
    assert data[day]["trade"]["dry_run"] is False and data[day]["trade"]["rc"] == 2


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


def test_write_heartbeat_marks_dry_run(tmp_path, monkeypatch):
    """心跳必须带 dry_run 标记并只留 14 天，watchdog 靠它区分"跑过"和"演练过"。"""
    import json
    mod = _load_script("live_auto.py")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    cfg = {"out_dir": "live"}
    mod.write_heartbeat(cfg, [("data", 0)], dry_run=True)
    mod.write_heartbeat(cfg, [("trade", 2)], dry_run=False)
    data = json.loads((tmp_path / "live" / "heartbeat.json").read_text(encoding="utf-8"))
    day = mod.today()
    assert data[day]["data"]["dry_run"] is True
    assert data[day]["trade"]["dry_run"] is False and data[day]["trade"]["rc"] == 2
