"""live_signal_hype 的安全口径：与等权信号对齐，不把基准代码当成禁买个股。"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import live_signal  # noqa: E402
import live_signal_hype as hype  # noqa: E402


def test_excluded_symbols_drop_the_index_code_and_keep_the_colliding_stock():
    """000852 既是中证1000 也是石化机械。剔的是指数 000300，不是这只股票。"""
    excluded = hype.live_excluded_symbols(["000300"])
    assert "000300" in excluded
    assert "000852" not in excluded
    # universe.yaml 没写 000300 时也要补上，和回测入口一样。
    assert hype.live_excluded_symbols([]) == ["000300"]
    assert hype.live_excluded_symbols(None) == ["000300"]


def test_stale_gate_uses_the_same_threshold_as_equal_weight():
    assert hype.MAX_STALE_DAYS == live_signal.MAX_STALE_DAYS == 15
    with pytest.raises(SystemExit, match="过期"):
        hype.assert_data_fresh("2020-01-02", today="2026-10-27")
    hype.assert_data_fresh("2026-10-20", today="2026-10-27")


def test_position_does_not_default_to_full_exposure():
    assert hype.resolve_position(None, 0.5) == pytest.approx(0.5)
    assert hype.resolve_position(0.4, 0.5) == pytest.approx(0.4)
    with pytest.raises(SystemExit, match="不会默认满仓"):
        hype.resolve_position(None, None)
    with pytest.raises(SystemExit, match="position"):
        hype.resolve_position(1.5, 0.5)
    with pytest.raises(SystemExit, match="position"):
        hype.resolve_position(0.0, 0.5)


def test_account_capital_does_not_add_holdings_on_top_of_total_equity():
    assert hype.account_capital(100_000, None, 50_000, 1_000_000) == 150_000
    # 调用方给的是总资产时，持仓市值已经在里面，不能再加一遍。
    assert hype.account_capital(None, 1_000_000, 50_000, 800_000) == 1_000_000
    assert hype.account_capital(None, None, 50_000, 1_000_000) == 1_000_000


def test_missing_price_refuses_to_size_orders():
    with pytest.raises(SystemExit, match="拒绝出单"):
        hype.require_prices(["600001"], {"600001": 0.0}, "持仓")
    with pytest.raises(SystemExit, match="600002"):
        hype.require_prices(["600002"], {}, "观察池")
    hype.require_prices([], {}, "持仓")
    hype.require_prices(["600001"], {"600001": 10.0}, "持仓")


def test_static_industry_proxy_refuses_a_historical_as_of():
    with pytest.raises(SystemExit, match="前视"):
        hype.refuse_static_theme_replay("2024-01-02", "2026-10-24")
    hype.refuse_static_theme_replay("2026-10-24", "2026-10-24")


def test_dry_run_does_not_write_orders_or_equity(tmp_path):
    orders = pd.DataFrame(
        [{"symbol": "600001", "side": "buy", "shares": 100,
          "price": 10.0, "amount": 1000.0, "note": ""}])
    log = pd.DataFrame([{"kind": "flat"}])
    summary = {"n_orders": 1}
    date_dir = tmp_path / "2026-10-24"
    eq = tmp_path / "equity_history.csv"
    assert hype.write_live_bundle(
        dry_run=True, date_dir=date_dir, orders_df=orders, log_df=log,
        summary=summary, eq_path=eq, equity_ts="2026-10-24", equity=1_000_000,
    ) is False
    assert not date_dir.exists()
    assert not eq.exists()
    assert hype.write_live_bundle(
        dry_run=False, date_dir=date_dir, orders_df=orders, log_df=log,
        summary=summary, eq_path=eq, equity_ts="2026-10-24", equity=1_000_000,
    ) is True
    assert (date_dir / "orders.csv").exists()
    assert (date_dir / "signal_log.csv").exists()
    assert (date_dir / "summary.json").exists()
    saved = pd.read_csv(eq)
    assert list(saved["equity"]) == [1_000_000]
