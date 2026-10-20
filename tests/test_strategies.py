

def test_equal_weight_uses_hash_sampling_not_code_order():
    """选股必须与代码字典序无关（F-05）：否则真实宇宙里永远取 000001~000030。"""
    from a_share_quant.strategies.equal_weight import EqualWeightStrategy

    universe = [f"{i:06d}" for i in range(1, 200)]
    strat = EqualWeightStrategy(max_positions=20)
    target = strat.generate_target("2024-01-02", None, universe)
    picked = sorted(target.weights)

    assert len(picked) == 20
    assert picked != sorted(universe)[:20]        # 不能等于字典序前 20
    assert set(picked) <= set(universe)
    assert abs(sum(target.weights.values()) - 1.0) < 1e-12


def test_equal_weight_selection_is_reproducible():
    """同一股票池、跨进程也应选出同一批（用 sha1，不用内置 hash）。"""
    from a_share_quant.strategies.equal_weight import EqualWeightStrategy

    universe = [f"{i:06d}" for i in range(1, 300)]
    a = EqualWeightStrategy(max_positions=10).generate_target("2024-01-02", None, universe)
    b = EqualWeightStrategy(max_positions=10).generate_target("2024-06-03", None, universe)
    assert sorted(a.weights) == sorted(b.weights)


def test_equal_weight_respects_universe_smaller_than_cap():
    from a_share_quant.strategies.equal_weight import EqualWeightStrategy

    target = EqualWeightStrategy(max_positions=50).generate_target(
        "2024-01-02", None, ["600000", "000001"])
    assert sorted(target.weights) == ["000001", "600000"]
    assert all(abs(w - 0.5) < 1e-12 for w in target.weights.values())
