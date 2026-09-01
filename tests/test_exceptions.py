"""engine/exceptions.py: the maximal-deterministic L4 classifier. One constructed
case per category, plus false-positive checks - a clean record must never become
an exception, and a genuinely unexplained one must show up in the residual set,
not get mis-forced into a category.
"""

from __future__ import annotations

from datetime import date

from engine.exceptions import build_ledger, classify_deterministic
from engine.fees import compute_expected_fee
from engine.io import BankRow, Dataset, OrderRow, PaymentRow, SettlementRow


def _order(order_id="order_1", gross_paise=100_000_00) -> OrderRow:
    return OrderRow(order_id, "2026-08-01", "cust_1", gross_paise, "INR", "upi", "paid", "INV-1")


def _payment(**overrides) -> PaymentRow:
    fee, gst, net = compute_expected_fee(100_000_00, "upi", False)
    defaults = dict(
        payment_id="pay_1", order_id="order_1", captured_at="2026-08-01T10:00:00", method="upi",
        international=False, gross_paise=100_000_00, fee_paise=fee, gst_paise=gst, net_paise=net,
        status="captured", settlement_id="setl_1", refund_id=None, refund_paise=0,
    )
    defaults.update(overrides)
    return PaymentRow(**defaults)


def _settlement(**overrides) -> SettlementRow:
    defaults = dict(
        settlement_id="setl_1", settled_at="2026-08-03", utr="HDFCN00000000001", num_payments=1,
        gross_paise=100_000_00, fee_paise=0, gst_paise=0, adjustment_paise=0, net_paise=97_640_00,
    )
    defaults.update(overrides)
    return SettlementRow(**defaults)


def _classify(orders=(), payments=(), settlements=(), credit_residual=(), debit_residual=(), as_of=None):
    dataset = Dataset(orders=list(orders), payments=list(payments), settlements=list(settlements), bank=[])
    return classify_deterministic(dataset, list(credit_residual), list(debit_residual), as_of=as_of)


def test_clean_record_raises_nothing():
    exceptions, unexplained = _classify(orders=[_order()], payments=[_payment()], settlements=[_settlement()])
    assert exceptions == []
    assert unexplained == set()


def test_rounding_drift_is_not_an_exception():
    """Within the +/-3p tolerance - resolvable, must never become an exception."""
    payment = _payment(net_paise=_payment().net_paise + 2)
    exceptions, unexplained = _classify(orders=[_order()], payments=[payment], settlements=[_settlement()])
    assert exceptions == []
    assert unexplained == set()


def test_missing_settlement():
    payment = _payment(settlement_id=None)
    exceptions, _ = _classify(orders=[_order()], payments=[payment])
    assert len(exceptions) == 1
    assert exceptions[0].category == "MISSING_SETTLEMENT"
    assert exceptions[0].affected == {"payment_id": "pay_1"}
    assert exceptions[0].recommended_action
    assert exceptions[0].evidence_chain


def test_aging_days_is_computed_from_the_payments_own_capture_date():
    # aging_days must never be a hardcoded 0 - it's a real field the brief's
    # own exception-ledger shape asks for, and the dashboard shows it.
    payment = _payment(settlement_id=None, captured_at="2026-08-01T10:00:00")
    exceptions, _ = _classify(orders=[_order()], payments=[payment], as_of=date(2026, 8, 15))
    assert exceptions[0].aging_days == 14


def test_aging_days_for_period_cutoff_uses_the_settlement_date_not_capture():
    settlement = _settlement(settled_at="2026-08-10")
    exceptions, _ = _classify(orders=[_order()], payments=[_payment()], settlements=[settlement], as_of=date(2026, 8, 20))
    assert exceptions[0].category == "PERIOD_CUTOFF"
    assert exceptions[0].aging_days == 10  # from settled_at (Aug 10), not captured_at (Aug 1)


def test_aging_days_for_orphan_chargeback_uses_the_bank_value_date():
    debit = BankRow("btxn_1", "2026-08-05", "CHARGEBACK DEBIT", 0, 3_000_00, 0)
    exceptions, _ = _classify(debit_residual=[debit], as_of=date(2026, 8, 12))
    assert exceptions[0].category == "ORPHAN_CHARGEBACK"
    assert exceptions[0].aging_days == 7


def test_aging_days_never_negative_even_if_as_of_predates_the_event():
    payment = _payment(settlement_id=None, captured_at="2026-08-10T00:00:00")
    exceptions, _ = _classify(orders=[_order()], payments=[payment], as_of=date(2026, 8, 1))
    assert exceptions[0].aging_days == 0


def test_duplicate_payment():
    original = _payment(payment_id="pay_orig", settlement_id="setl_1")
    duplicate = _payment(payment_id="pay_dup", settlement_id=None)
    exceptions, _ = _classify(orders=[_order()], payments=[original, duplicate])
    assert len(exceptions) == 1
    assert exceptions[0].category == "DUPLICATE_PAYMENT"
    assert exceptions[0].affected["payment_id"] == "pay_dup"


def test_fee_variance_wrong_tier():
    _, _, wrong_net = compute_expected_fee(100_000_00, "card", False)  # 2% MDR applied to a UPI payment
    payment = _payment(net_paise=wrong_net)
    exceptions, unexplained = _classify(orders=[_order()], payments=[payment], settlements=[_settlement()])
    assert len(exceptions) == 1
    assert exceptions[0].category == "FEE_VARIANCE"
    assert unexplained == set()


def test_tax_variance_wrong_gst_rate():
    from engine.fees import round_half_up_div
    fee, correct_gst, correct_net = compute_expected_fee(100_000_00, "card", False)
    wrong_gst = round_half_up_div(fee * 2800, 10_000)
    assert wrong_gst != correct_gst
    payment = _payment(method="card", fee_paise=fee, gst_paise=wrong_gst, net_paise=100_000_00 - fee - wrong_gst)
    exceptions, _ = _classify(orders=[_order()], payments=[payment], settlements=[_settlement()])
    assert len(exceptions) == 1
    assert exceptions[0].category == "TAX_VARIANCE"


def test_refund_misallocation():
    wrong_order = _order(order_id="order_wrong", gross_paise=50_000_00)
    payment = _payment(order_id="order_wrong", refund_id="rfnd_1", refund_paise=20_000_00)
    exceptions, unexplained = _classify(orders=[wrong_order], payments=[payment], settlements=[_settlement()])
    assert len(exceptions) == 1
    assert exceptions[0].category == "REFUND_MISALLOCATION"
    assert exceptions[0].amount_at_risk_paise == 20_000_00
    assert unexplained == set()


def test_fx_variance():
    order = _order(order_id="order_1", gross_paise=100_000_00)
    drifted_gross = 102_500_00
    fee, gst, net = compute_expected_fee(drifted_gross, "card", True)
    payment = _payment(method="card", international=True, gross_paise=drifted_gross, fee_paise=fee, gst_paise=gst, net_paise=net)
    exceptions, unexplained = _classify(orders=[order], payments=[payment], settlements=[_settlement()])
    assert len(exceptions) == 1
    assert exceptions[0].category == "FX_VARIANCE"
    assert exceptions[0].amount_at_risk_paise == 2_500_00
    assert unexplained == set()


def test_period_cutoff_beyond_threshold():
    settlement = _settlement(settled_at="2026-08-10")  # 9 days after capture - well beyond the 4-day threshold
    exceptions, _ = _classify(orders=[_order()], payments=[_payment()], settlements=[settlement])
    assert len(exceptions) == 1
    assert exceptions[0].category == "PERIOD_CUTOFF"


def test_period_cutoff_within_normal_range_is_not_flagged():
    settlement = _settlement(settled_at="2026-08-04")  # 3 days - within the observed normal T+N range
    exceptions, _ = _classify(orders=[_order()], payments=[_payment()], settlements=[settlement])
    assert exceptions == []


def test_orphan_chargeback():
    debit = BankRow("btxn_1", "2026-08-05", "CHARGEBACK DR-DSPZZZZZZZZ-RAZORPAY SOFTWARE", 0, 500_00, 0)
    exceptions, _ = _classify(debit_residual=[debit])
    assert len(exceptions) == 1
    assert exceptions[0].category == "ORPHAN_CHARGEBACK"
    assert exceptions[0].amount_at_risk_paise == 500_00


def test_unidentified_credit():
    credit = BankRow("btxn_1", "2026-08-05", "NEFT-XXXX-R KUMAR", 5_000_00, 0, 0)
    exceptions, _ = _classify(credit_residual=[credit])
    assert len(exceptions) == 1
    assert exceptions[0].category == "UNIDENTIFIED_CREDIT"


def test_genuinely_unexplained_variance_is_residual_not_a_category():
    payment = _payment(net_paise=_payment().net_paise - 12_345)  # doesn't fit any known hypothesis
    exceptions, unexplained = _classify(orders=[_order()], payments=[payment], settlements=[_settlement()])
    assert exceptions == []
    assert unexplained == {"pay_1"}


def test_high_value_exception_gets_review_required_severity():
    payment = _payment(settlement_id=None, net_paise=60_000_00)
    exceptions, _ = _classify(orders=[_order()], payments=[payment])
    assert exceptions[0].severity == "REVIEW_REQUIRED"


def test_low_value_exception_gets_standard_severity():
    payment = _payment(settlement_id=None, net_paise=5_000_00)
    exceptions, _ = _classify(orders=[_order()], payments=[payment])
    assert exceptions[0].severity == "STANDARD"


def test_build_ledger_sorts_by_amount_at_risk_descending():
    small = _payment(payment_id="pay_small", settlement_id=None, net_paise=1_000_00)
    big = _payment(payment_id="pay_big", settlement_id=None, net_paise=99_000_00)
    order_small = _order(order_id="order_small")
    order_big = _order(order_id="order_big")
    small = PaymentRow(**{**small.__dict__, "order_id": "order_small"})
    big = PaymentRow(**{**big.__dict__, "order_id": "order_big"})

    exceptions, _ = _classify(orders=[order_small, order_big], payments=[small, big])
    ledger = build_ledger(exceptions)
    assert [e.amount_at_risk_paise for e in ledger] == sorted(
        (e.amount_at_risk_paise for e in exceptions), reverse=True
    )
    assert ledger[0].affected["payment_id"] == "pay_big"


def test_suggested_owner_is_real_and_differentiated_by_category():
    # suggested_owner must never be a uniform constant across every category -
    # same bug shape as aging_days/cost_usd_micros: a field that exists but
    # carries no information because nothing ever differentiates it.
    order = _order(order_id="order_1", gross_paise=100_000_00)
    drifted_gross = 102_500_00
    fee, gst, net = compute_expected_fee(drifted_gross, "card", True)
    fx_payment = _payment(method="card", international=True, gross_paise=drifted_gross, fee_paise=fee, gst_paise=gst, net_paise=net)
    exceptions, _ = _classify(orders=[order], payments=[fx_payment], settlements=[_settlement()])
    assert exceptions[0].category == "FX_VARIANCE"
    assert exceptions[0].suggested_owner == "Treasury"

    debit = BankRow("btxn_1", "2026-08-05", "CHARGEBACK DEBIT", 0, 3_000_00, 0)
    exceptions, _ = _classify(debit_residual=[debit])
    assert exceptions[0].category == "ORPHAN_CHARGEBACK"
    assert exceptions[0].suggested_owner == "Disputes & Risk"

    missing = _payment(settlement_id=None)
    exceptions, _ = _classify(orders=[_order()], payments=[missing])
    assert exceptions[0].category == "MISSING_SETTLEMENT"
    assert exceptions[0].suggested_owner == "Payments Ops"
    # a genuinely different owner than the FX/chargeback cases above -
    # the whole point is these are not all the same string
    assert len({"Treasury", "Disputes & Risk", "Payments Ops"}) == 3
