from data.generator.fees import compute_expected_fee, mdr_bps, round_half_up_div


def test_round_half_up_not_banker():
    # Banker's rounding would take 5 -> 0 (round to even). Half-up takes it to 1.
    assert round_half_up_div(5, 10) == 1
    assert round_half_up_div(15, 10) == 2
    assert round_half_up_div(25, 10) == 3


def test_round_half_up_negative_symmetry():
    assert round_half_up_div(-5, 10) == -1
    assert round_half_up_div(-15, 10) == -2


def test_zero_mdr_methods_net_equals_gross():
    for method in ("upi", "rupay_debit"):
        fee, gst, net = compute_expected_fee(100_000, method, False)
        assert (fee, gst, net) == (0, 0, 100_000)


def test_card_domestic_2pct_plus_18pct_gst():
    fee, gst, net = compute_expected_fee(100_000, "card", False)
    assert fee == 2_000          # 2.00% of 100000
    assert gst == 360            # 18% of 2000
    assert net == 100_000 - 2_000 - 360


def test_card_international_3pct():
    fee, gst, net = compute_expected_fee(500_000, "card", True)
    assert fee == 15_000
    assert gst == round_half_up_div(15_000 * 1800, 10_000)
    assert net == 500_000 - fee - gst


def test_netbanking_190bps():
    fee, gst, net = compute_expected_fee(1_000_00, "netbanking", False)
    assert fee == round_half_up_div(1_000_00 * 190, 10_000)


def test_fee_never_exceeds_gross():
    for method in ("upi", "rupay_debit", "card", "netbanking", "wallet"):
        for intl in (False, True) if method == "card" else (False,):
            fee, gst, net = compute_expected_fee(1, method, intl)
            assert net >= 0


def test_unknown_tier_raises():
    import pytest
    from data.generator.fees import UnknownFeeTier

    with pytest.raises(UnknownFeeTier):
        mdr_bps("bitcoin", False)
