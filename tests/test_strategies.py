

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


def test_seed_zero_keeps_original_hash_order():
    """seed=0 必须沿用 sha1(symbol)，否则历史实验无法复现。

    加 seed 参数时给 key 加了前缀，种子 0 的持仓被静默换掉，
    年化从 9.15% 变成 4.55%，而代码看起来只多了一个默认参数。
    """
    import hashlib

    from a_share_quant.strategies.equal_weight import EqualWeightStrategy

    universe = [f"{i:06d}" for i in range(1, 300)]
    expected = sorted(universe, key=lambda s: hashlib.sha1(s.encode()).hexdigest())[:20]
    got = sorted(EqualWeightStrategy(max_positions=20, seed=0)
                 .generate_target("2024-01-02", None, universe).weights)
    # 注意：target.weights 是 dict，sorted() 排的是键名（代码升序），
    # 所以两侧都要排过再比，不能拿"选中顺序"的列表直接比。
    assert got == sorted(expected)

    # seed>0 必须真的换一批，否则"换种子检验运气"没有意义
    other = sorted(EqualWeightStrategy(max_positions=20, seed=1)
                   .generate_target("2024-01-02", None, universe).weights)
    assert other != got
