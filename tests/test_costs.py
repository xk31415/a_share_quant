"""交易成本测试：佣金、印花税、滑点。"""
import pytest

from a_share_quant.backtest.costs import CostModel


def test_commission_minimum_applied():
    cm = CostModel(commission_rate=0.00025, commission_min=5.0)
    # 10000 * 0.00025 = 2.5 < 5 -> 取最低佣金
    assert cm.buy_cost(10_000) == 5.0
    assert cm.sell_cost(10_000) == pytest.approx(5.0 + 10_000 * 0.0005)


def test_commission_proportional_when_above_minimum():
    cm = CostModel(commission_rate=0.00025, commission_min=5.0)
    assert cm.buy_cost(100_000) == pytest.approx(25.0)


def test_stamp_duty_sell_only():
    cm = CostModel(commission_rate=0.0, commission_min=0.0,
                   stamp_duty_rate=0.001, slippage_rate=0.0)
    assert cm.buy_cost(10_000) == 0.0
    assert cm.sell_cost(10_000) == pytest.approx(10.0)


def test_slippage_adverse_direction():
    cm = CostModel(slippage_rate=0.001)
    assert cm.buy_price(100.0) == pytest.approx(100.1)
    assert cm.sell_price(100.0) == pytest.approx(99.9)


def test_zero_turnover_has_zero_cost():
    cm = CostModel()
    assert cm.buy_cost(0.0) == 0.0
    assert cm.sell_cost(0.0) == 0.0


def test_invalid_rates_rejected():
    with pytest.raises(ValueError):
        CostModel(commission_rate=0.5)
    with pytest.raises(ValueError):
        CostModel(commission_min=-1.0)
