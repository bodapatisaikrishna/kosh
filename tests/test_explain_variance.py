"""engine/fees.py:explain_variance is what turns "these numbers differ" into "these
numbers differ because" - the honesty of the eventual exception ledger depends on it
telling the truth, including saying UNEXPLAINED when it genuinely doesn't know.
"""

from __future__ import annotations

from engine.fees import GST_BPS, compute_expected_fee, explain_variance, round_half_up_div


def test_exact_match():
    e = explain_variance(97640, 97640)
    assert e.cause == "MATCH"
    assert e.delta_paise == 0


def test_rounding_drift_within_tolerance():
    for drift in (-3, -2, -1, 1, 2, 3):
        e = explain_variance(100_000 + drift, 100_000)
        assert e.cause == "ROUNDING", drift
        assert e.delta_paise == drift


def test_delta_of_four_paise_is_not_rounding():
    e = explain_variance(100_004, 100_000, gross_paise=1_000_000, method="upi", international=False)
    assert e.cause != "ROUNDING"


def test_known_refund_explains_the_delta():
    e = explain_variance(90_000, 100_000, known_refund_paise=10_000)
    assert e.cause == "REFUND"
    assert e.delta_paise == -10_000


def test_wrong_fee_tier_is_detected():
    gross = 1_000_00
    _, _, correct_net = compute_expected_fee(gross, "upi", False)  # zero-MDR
    _, _, wrong_net = compute_expected_fee(gross, "card", False)  # 2% MDR applied by mistake
    e = explain_variance(wrong_net, correct_net, gross_paise=gross, method="upi", international=False)
    assert e.cause == "FEE_TIER"
    assert "card" in e.detail


def test_wrong_gst_rate_is_detected():
    gross = 1_000_00
    fee, correct_gst, correct_net = compute_expected_fee(gross, "card", False)
    wrong_gst = round_half_up_div(fee * 2800, 10_000)
    wrong_net = gross - fee - wrong_gst
    assert wrong_gst != correct_gst

    e = explain_variance(wrong_net, correct_net, gross_paise=gross, method="card", international=False)
    assert e.cause == "GST_RATE"
    assert "2800" in e.detail and str(GST_BPS) in e.detail


def test_truly_unexplained_delta_says_so_honestly():
    e = explain_variance(50_000, 100_000, gross_paise=1_000_00, method="upi", international=False)
    assert e.cause == "UNEXPLAINED"
    assert e.delta_paise == -50_000


def test_unexplained_when_no_context_is_given_at_all():
    """Without gross/method, fee-tier and GST-rate hypotheses can't even be tested -
    this must fall through to UNEXPLAINED, not silently pick a wrong cause."""
    e = explain_variance(12_345, 99_999)
    assert e.cause == "UNEXPLAINED"
