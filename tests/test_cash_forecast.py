"""cash/forecast.py: the reconciled-cash-vs-book-cash identity must tie to the
paisa on real data - "if those don't tie, you have a bug" per the brief - plus
the inflow curve, stuck detection, and as-of-date inference on constructed
cases.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from cash.forecast import (
    _infer_as_of_date,
    compute_cash_reconciliation,
    compute_forecast,
    compute_inflow_curve,
    compute_stuck,
)
from engine.contract import Match, ReconException
from engine.io import BankRow, Dataset, OrderRow, PaymentRow, SettlementRow, load_dataset
from engine.pipeline import run_full

FIXTURES_2000 = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "run_2000"
FIXTURES_200 = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "sample_200"


def _payment(**overrides) -> PaymentRow:
    defaults = dict(
        payment_id="pay_1", order_id="order_1", captured_at="2026-08-01T10:00:00", method="upi",
        international=False, gross_paise=100_00, fee_paise=0, gst_paise=0, net_paise=100_00,
        status="captured", settlement_id=None, refund_id=None, refund_paise=0,
    )
    defaults.update(overrides)
    return PaymentRow(**defaults)


# --- the reconciliation identity, on real data ---------------------------------

def test_reconciliation_ties_exactly_on_run_2000():
    dataset = load_dataset(FIXTURES_2000)
    output = run_full(dataset, client=None)
    book, reconciled, components = compute_cash_reconciliation(dataset, output.matches, output.exceptions)
    assert (book - reconciled) - sum(components.values()) == 0


def test_reconciliation_ties_exactly_on_sample_200():
    dataset = load_dataset(FIXTURES_200)
    output = run_full(dataset, client=None)
    book, reconciled, components = compute_cash_reconciliation(dataset, output.matches, output.exceptions)
    assert (book - reconciled) - sum(components.values()) == 0


# --- inflow curve ----------------------------------------------------------------

def test_inflow_curve_places_unsettled_payment_on_its_expected_settlement_date():
    # 2026-08-04 is a Tuesday: UPI's T+1 lands on Wednesday 08-05 with no
    # weekend/holiday rollover to reason about.
    payment = _payment(method="upi", captured_at="2026-08-04T10:00:00", net_paise=500_00)
    dataset = Dataset(orders=[], payments=[payment], settlements=[], bank=[])
    curve = compute_inflow_curve(dataset, as_of_date=date(2026, 8, 4))
    by_date = {row["date"]: row["expected_inflow_paise"] for row in curve}
    assert by_date["2026-08-05"] == 500_00
    assert sum(by_date.values()) == 500_00


def test_inflow_curve_excludes_already_settled_payments():
    payment = _payment(settlement_id="setl_1", net_paise=500_00)
    dataset = Dataset(orders=[], payments=[payment], settlements=[], bank=[])
    curve = compute_inflow_curve(dataset, as_of_date=date(2026, 8, 1))
    assert sum(row["expected_inflow_paise"] for row in curve) == 0


def test_inflow_curve_is_fourteen_days():
    dataset = Dataset(orders=[], payments=[], settlements=[], bank=[])
    curve = compute_inflow_curve(dataset, as_of_date=date(2026, 8, 1))
    assert len(curve) == 14
    assert curve[0]["date"] == "2026-08-01"
    assert curve[-1]["date"] == "2026-08-14"


# --- stuck -------------------------------------------------------------------

def test_payment_within_normal_sla_is_not_stuck():
    # UPI T+1 from Tuesday 08-04 is due 08-05; +1 grace day is the boundary 08-06.
    payment = _payment(method="upi", captured_at="2026-08-04T10:00:00")
    dataset = Dataset(orders=[], payments=[payment], settlements=[], bank=[])
    stuck_paise, stuck_ids = compute_stuck(dataset, as_of_date=date(2026, 8, 6))  # exactly on the grace boundary
    assert stuck_paise == 0
    assert stuck_ids == []


def test_payment_past_sla_plus_grace_is_stuck():
    payment = _payment(method="upi", captured_at="2026-08-04T10:00:00", net_paise=250_00)  # due 08-05, +1 grace = 08-06
    dataset = Dataset(orders=[], payments=[payment], settlements=[], bank=[])
    stuck_paise, stuck_ids = compute_stuck(dataset, as_of_date=date(2026, 8, 7))
    assert stuck_paise == 250_00
    assert stuck_ids == ["pay_1"]


def test_settled_payment_is_never_stuck():
    payment = _payment(settlement_id="setl_1", captured_at="2026-01-01T10:00:00")
    dataset = Dataset(orders=[], payments=[payment], settlements=[], bank=[])
    stuck_paise, stuck_ids = compute_stuck(dataset, as_of_date=date(2026, 8, 1))
    assert stuck_paise == 0
    assert stuck_ids == []


# --- as-of-date inference -----------------------------------------------------

def test_as_of_date_uses_only_capture_dates_not_settlement_or_bank_dates():
    payment = _payment(captured_at="2026-08-01T10:00:00")
    settlement = SettlementRow("setl_1", "2026-08-10", "HDFCN00000000001", 1, 100_00, 0, 0, 0, 100_00)
    bank = BankRow("btxn_1", "2026-08-10", "NEFT-HDFCN00000000001-RAZORPAY SOFTWARE PVT LTD", 100_00, 0, 0)
    dataset = Dataset(orders=[], payments=[payment], settlements=[settlement], bank=[bank])
    assert _infer_as_of_date(dataset) == date(2026, 8, 1)


# --- reconciliation components, on a constructed case --------------------------

def test_reconciliation_components_on_a_hand_built_case():
    """One settled payment (with a small refund and a settlement adjustment),
    one unsettled payment, one legitimate chargeback, one orphan chargeback
    exception, one unidentified credit exception - every component exercised
    and the identity verified by hand."""
    order1 = OrderRow("order_1", "2026-08-01", "cust_1", 100_000_00, "INR", "upi", "paid", "INV-1")
    order2 = OrderRow("order_2", "2026-08-01", "cust_2", 50_000_00, "INR", "upi", "paid", "INV-2")
    # payment.net_paise is gross - fee - gst only - refund is a separate field,
    # subtracted only at the settlement level (matching data/generator/world.py's
    # own convention). Baking it into payment.net_paise too would double-count
    # it against settled_refunds_paise below.
    settled_payment = _payment(
        payment_id="pay_settled", order_id="order_1", settlement_id="setl_1",
        gross_paise=100_000_00, fee_paise=2_000_00, gst_paise=360_00, net_paise=97_640_00,
        refund_id="rfnd_1", refund_paise=500_00,
    )
    unsettled_payment = _payment(payment_id="pay_unsettled", order_id="order_2", gross_paise=50_000_00, net_paise=50_000_00, settlement_id=None)
    settlement = SettlementRow("setl_1", "2026-08-03", "HDFCN00000000001", 1, 100_000_00, 2_000_00, 360_00, 10_000, 97_640_00 - 500_00 + 10_000)
    settlement_credit = BankRow("btxn_settle", "2026-08-03", "NEFT-HDFCN00000000001-RAZORPAY SOFTWARE PVT LTD", 97_640_00 - 500_00 + 10_000, 0, 0)
    chargeback_debit = BankRow("btxn_chargeback", "2026-08-05", "CHARGEBACK DR-ref-RAZORPAY SOFTWARE", 0, 5_000_00, 0)
    # An exception's amount_at_risk_paise always traces back to a real bank row
    # in the actual pipeline (engine/exceptions.py reads it straight off the
    # residual BankRow) - these two exist so reconciled_cash_paise (summed
    # from dataset.bank) actually reflects them, not just the exception list.
    orphan_debit = BankRow("btxn_orphan", "2026-08-05", "CHARGEBACK DR-orphan-RAZORPAY SOFTWARE", 0, 300_00, 0)
    unidentified_credit_txn = BankRow("btxn_unidentified", "2026-08-05", "NEFT-XXXX-R KUMAR", 1_000_00, 0, 0)

    dataset = Dataset(
        orders=[order1, order2],
        payments=[settled_payment, unsettled_payment],
        settlements=[settlement],
        bank=[settlement_credit, chargeback_debit, orphan_debit, unidentified_credit_txn],
    )
    matches = [Match(layer="L0", link_type="chargeback_payment", left_id="pay_settled", right_id="btxn_chargeback", confidence=1.0)]
    exceptions = [
        ReconException(category="ORPHAN_CHARGEBACK", severity="STANDARD", amount_at_risk_paise=300_00, affected={"bank_txn_id": "btxn_orphan"}, recommended_action="x"),
        ReconException(category="UNIDENTIFIED_CREDIT", severity="STANDARD", amount_at_risk_paise=1_000_00, affected={"bank_txn_id": "btxn_unidentified"}, recommended_action="x"),
    ]

    book, reconciled, components = compute_cash_reconciliation(dataset, matches, exceptions)

    assert book == 100_000_00 + 50_000_00
    assert components["unsettled_gross_paise"] == 50_000_00
    assert components["settled_gross_minus_net_paise"] == 100_000_00 - 97_640_00
    assert components["settled_refunds_paise"] == 500_00
    assert components["adjustments_paise"] == -10_000
    assert components["legitimate_chargeback_paise"] == 5_000_00
    assert components["orphan_chargeback_paise"] == 300_00
    assert components["unidentified_credit_paise"] == -1_000_00
    assert (book - reconciled) - sum(components.values()) == 0


def test_compute_forecast_end_to_end_shape():
    dataset = load_dataset(FIXTURES_200)
    output = run_full(dataset, client=None)
    forecast = compute_forecast(dataset, output.matches, output.exceptions)
    assert len(forecast.inflow_curve) == 14
    assert forecast.at_risk_paise == sum(e.amount_at_risk_paise for e in output.exceptions)
    assert (forecast.book_cash_paise - forecast.reconciled_cash_paise) - sum(forecast.reconciliation.values()) == 0
