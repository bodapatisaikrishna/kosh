"""Phase 3 checkpoint: L0 deterministic joins + L1 tolerance matching.

    auto-match rate >= 88%
    false_match_rate == 0.00%     - fix before adding anything else
    runtime for 2000 records < 5 seconds

Also covers the two "never guess" ambiguity guards directly (a truncated UTR prefix
shared by more than one settlement; two settlements both within L1's amount/date/
narration tolerance of one bank credit) since those are exactly the situations that
would otherwise produce a false match, and the reference fixture may not happen to
exercise them.
"""

from __future__ import annotations

from pathlib import Path

from data.generator.ids import derive_dispute_ref
from engine.io import BankRow, Dataset, PaymentRow, SettlementRow
from engine.io import load_dataset
from engine.l0_deterministic import match_chargeback_payment, match_settlement_bank_txn
from engine.l1_tolerance import match_settlement_bank_txn as l1_match_settlement_bank_txn
from engine.pipeline import run_l0_l1
from eval.io import load_ground_truth
from eval.metrics import compute_metrics

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "run_2000"


def test_phase3_checkpoint_on_run_2000():
    dataset = load_dataset(FIXTURES)
    ground_truth = load_ground_truth(FIXTURES)

    output = run_l0_l1(dataset)
    metrics = compute_metrics(dataset, output, ground_truth)

    assert metrics["accuracy"]["auto_match_rate"] >= 0.88
    assert metrics["accuracy"]["false_match_rate"] == 0.0
    assert output.meta.wall_clock_seconds < 5.0


def test_l0_never_guesses_an_ambiguous_utr_prefix():
    """Two settlements whose UTRs share a 12+ char prefix, and a bank credit whose
    narration was truncated down to just that shared prefix: L0 must refuse to pick
    either one rather than assert a coin-flip match."""
    settlements = [
        SettlementRow("setl_a", "2026-08-01", "HDFCN12345678901", 1, 0, 0, 0, 0, 100_00),
        SettlementRow("setl_b", "2026-08-01", "HDFCN12345678999", 1, 0, 0, 0, 0, 200_00),
    ]
    bank = [BankRow("btxn_1", "2026-08-01", "NEFT-HDFCN123456789", 100_00, 0, 0)]
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=bank)

    matches, residual = match_settlement_bank_txn(dataset)
    assert matches == []
    assert residual == bank


def test_l0_exact_utr_still_matches_when_a_similar_prefix_exists_elsewhere():
    """The ambiguity guard must not become trigger-happy: an exact 16-char UTR
    match is unaffected by an unrelated settlement merely sharing a prefix."""
    settlements = [
        SettlementRow("setl_a", "2026-08-01", "HDFCN12345678901", 1, 0, 0, 0, 0, 100_00),
        SettlementRow("setl_b", "2026-08-01", "HDFCN12345678999", 1, 0, 0, 0, 0, 200_00),
    ]
    bank = [BankRow("btxn_1", "2026-08-01", "NEFT-HDFCN12345678901-RAZORPAY SOFTWARE PVT LTD", 100_00, 0, 0)]
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=bank)

    matches, residual = match_settlement_bank_txn(dataset)
    assert len(matches) == 1
    assert matches[0].left_id == "setl_a"
    assert residual == []


def test_l1_never_guesses_between_two_equally_plausible_settlements():
    """Two settlements both within amount+date tolerance of one residual bank
    credit: L1 must escalate (assert nothing), not pick the closer one."""
    settlements = [
        SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 50_000_00),
        SettlementRow("setl_b", "2026-08-02", "ICICN00000000002", 1, 0, 0, 0, 0, 50_001_00),
    ]
    txn = BankRow("btxn_1", "2026-08-01", "NEFT-XXXX-RAZORPAY SOFTWARE PVT LTD", 50_000_50, 0, 0)
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=[txn])

    matches = l1_match_settlement_bank_txn(dataset, [txn])
    assert matches == []


def test_l1_matches_the_unique_candidate_within_tolerance():
    settlements = [
        SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 50_000_00),
        SettlementRow("setl_b", "2026-08-10", "ICICN00000000002", 1, 0, 0, 0, 0, 90_000_00),
    ]
    txn = BankRow("btxn_1", "2026-08-02", "NEFT-XXXX-RAZORPAY SOFTWARE PVT LTD", 50_000_50, 0, 0)
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=[txn])

    matches = l1_match_settlement_bank_txn(dataset, [txn])
    assert len(matches) == 1
    assert matches[0].left_id == "setl_a"
    assert matches[0].layer == "L1"


def test_l1_rejects_a_close_amount_match_with_non_settlement_narration():
    """Amount+date alone must not be enough - a customer-name narration must fail
    the settlement-vocabulary gate even if it happens to land in tolerance."""
    settlements = [SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 50_000_00)]
    txn = BankRow("btxn_1", "2026-08-01", "NEFT-XXXX-R KUMAR", 50_000_00, 0, 0)
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=[txn])

    matches = l1_match_settlement_bank_txn(dataset, [txn])
    assert matches == []


def test_zero_net_settlement_credit_is_still_matched():
    """A settlement can legitimately net to Rs 0 - the resulting bank credit must
    not be mistaken for a debit and skipped."""
    settlements = [SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 0)]
    bank = [BankRow("btxn_1", "2026-08-01", "NEFT-HDFCN00000000001-RAZORPAY SOFTWARE PVT LTD", 0, 0, 0)]
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=bank)

    matches, residual = match_settlement_bank_txn(dataset)
    assert len(matches) == 1
    assert residual == []


def test_a_real_debit_is_never_a_settlement_candidate():
    settlements = [SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 5_000_00)]
    bank = [BankRow("btxn_1", "2026-08-01", "CHARGEBACK DR-ref-RAZORPAY SOFTWARE", 0, 5_000_00, 0)]
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=bank)

    matches, residual = match_settlement_bank_txn(dataset)
    assert matches == []
    assert residual == []  # a genuine debit is excluded outright, not left as a residual credit candidate


# --- L0 chargeback-to-payment matching -----------------------------------

def _payment(payment_id: str) -> PaymentRow:
    return PaymentRow(
        payment_id=payment_id, order_id="order_x", captured_at="2026-08-01T10:00:00", method="upi",
        international=False, gross_paise=100_00, fee_paise=0, gst_paise=0, net_paise=100_00,
        status="captured", settlement_id="setl_x", refund_id=None, refund_paise=0,
    )


def test_legitimate_chargeback_matches_its_payment():
    payment = _payment("pay_ABCDEFGHIJKLMN")
    ref = derive_dispute_ref(payment.payment_id)
    debit = BankRow("btxn_1", "2026-08-05", f"CHARGEBACK DR-{ref}-RAZORPAY SOFTWARE", 0, 500_00, 0)
    dataset = Dataset(orders=[], payments=[payment], settlements=[], bank=[debit])

    matches, residual = match_chargeback_payment(dataset)
    assert len(matches) == 1
    assert matches[0].left_id == payment.payment_id
    assert matches[0].right_id == "btxn_1"
    assert matches[0].link_type == "chargeback_payment"
    assert residual == []


def test_orphan_chargeback_never_guesses_a_payment():
    payment = _payment("pay_ABCDEFGHIJKLMN")
    debit = BankRow("btxn_1", "2026-08-05", "CHARGEBACK DR-DSPZZZZZZZZ-RAZORPAY SOFTWARE", 0, 500_00, 0)
    dataset = Dataset(orders=[], payments=[payment], settlements=[], bank=[debit])

    matches, residual = match_chargeback_payment(dataset)
    assert matches == []
    assert residual == [debit]


def test_credit_rows_are_never_chargeback_candidates():
    payment = _payment("pay_ABCDEFGHIJKLMN")
    ref = derive_dispute_ref(payment.payment_id)
    # Same reference text, but this is a credit, not a debit - must never match.
    credit = BankRow("btxn_1", "2026-08-05", f"NEFT-{ref}-RAZORPAY SOFTWARE", 500_00, 0, 0)
    dataset = Dataset(orders=[], payments=[payment], settlements=[], bank=[credit])

    matches, residual = match_chargeback_payment(dataset)
    assert matches == []
    assert residual == []
