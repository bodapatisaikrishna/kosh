"""Task 3: adversarial red-team suite.

The claim "false-match rate 0.00%" has been measured against thousands of
generator-produced records, but never deliberately attacked. Each function
here builds a small, hand-crafted Dataset engineered to induce a false match
at a specific layer, runs it through the real deterministic pipeline
(client=None - these are all L0/L1/L2 attacks, no LLM involved), and reports
one of exactly three outcomes:

    REFUSED     - the engine asserted no wrong link (an honest exception instead)
    CORRECT     - the engine asserted the one genuinely true link
    FALSE_MATCH - the engine asserted a wrong link

FALSE_MATCH is a real bug - see run_all_attacks()'s docstring. Every attack
here was verified by hand (not just assumed to work) before being encoded as
a permanent regression check: attack f originally surfaced a genuine bug
(a settlement double-claimed across two bank rows sharing a UTR), fixed in
engine/pipeline.py::_reconcile_settlement_credit_sums - see ARCHITECTURE.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.fees import compute_expected_fee
from engine.io import BankRow, Dataset, OrderRow, PaymentRow, SettlementRow
from engine.pipeline import run_full


@dataclass
class AttackResult:
    attack: str
    description: str
    outcome: str  # "REFUSED" | "CORRECT" | "FALSE_MATCH"
    detail: str


def _order_and_payment(order_id: str, payment_id: str, gross: int, settlement_id: str | None, captured: str = "2026-08-01T10:00:00", method: str = "upi") -> tuple[OrderRow, PaymentRow]:
    order = OrderRow(order_id, "2026-08-01", "cust_1", gross, "INR", method, "paid", f"INV-{order_id}")
    payment = PaymentRow(payment_id, order_id, captured, method, False, gross, 0, 0, gross, "captured", settlement_id, None, 0)
    return order, payment


def attack_a_rekeyed_utr_prefix() -> AttackResult:
    """Two settlements, identical amount/date, UTRs differing only in the last
    two digits (transposed). The bank narration carries only a truncated,
    14-char prefix shared by both - ambiguous, must never guess between them."""
    utr_a = "HDFCN12345678901"
    utr_b = "HDFCN12345678910"  # last two digits transposed
    o1, p1 = _order_and_payment("order_a1", "pay_a1", 100_000_00, "setl_a1")
    o2, p2 = _order_and_payment("order_a2", "pay_a2", 100_000_00, "setl_a2")
    s1 = SettlementRow("setl_a1", "2026-08-03", utr_a, 1, 100_000_00, 0, 0, 0, 100_000_00)
    s2 = SettlementRow("setl_a2", "2026-08-03", utr_b, 1, 100_000_00, 0, 0, 0, 100_000_00)
    bank = BankRow("btxn_a1", "2026-08-03", f"NEFT-{utr_a[:14]}-RAZORPAY SOFTWARE PVT LTD", 100_000_00, 0, 0)
    dataset = Dataset(orders=[o1, o2], payments=[p1, p2], settlements=[s1, s2], bank=[bank])

    output = run_full(dataset, client=None)
    sb_matches = [m for m in output.matches if m.link_type == "settlement_bank_txn" and m.right_id == "btxn_a1"]
    if not sb_matches:
        return AttackResult("a", "Rekeyed UTR prefix (L0/L1 ambiguity)", "REFUSED", "btxn_a1 asserted to neither settlement")
    return AttackResult("a", "Rekeyed UTR prefix (L0/L1 ambiguity)", "FALSE_MATCH", f"btxn_a1 wrongly asserted to {sb_matches[0].left_id}")


def attack_b_subset_sum_across_date_window() -> AttackResult:
    """A bank credit's amount coincidentally equals the sum of two UNRELATED
    settlements dated 60 days earlier - well outside L2's date window. They
    must never be considered candidates just because the arithmetic works."""
    o1, p1 = _order_and_payment("order_b1", "pay_b1", 60_000_00, "setl_b1", captured="2026-07-01T10:00:00")
    o2, p2 = _order_and_payment("order_b2", "pay_b2", 40_000_00, "setl_b2", captured="2026-07-01T10:00:00")
    s1 = SettlementRow("setl_b1", "2026-07-01", "HDFCN00000000101", 1, 60_000_00, 0, 0, 0, 60_000_00)
    s2 = SettlementRow("setl_b2", "2026-07-01", "HDFCN00000000102", 1, 40_000_00, 0, 0, 0, 40_000_00)
    bank = BankRow("btxn_b1", "2026-08-30", "NEFT-UNKNOWN REF-RAZORPAY SOFTWARE PVT LTD", 100_000_00, 0, 0)
    dataset = Dataset(orders=[o1, o2], payments=[p1, p2], settlements=[s1, s2], bank=[bank])

    output = run_full(dataset, client=None)
    sb_matches = [m for m in output.matches if m.link_type == "settlement_bank_txn" and m.right_id == "btxn_b1"]
    if not sb_matches:
        return AttackResult("b", "Subset-sum coincidence across L2's date window", "REFUSED", "btxn_b1 not matched to either out-of-window settlement")
    return AttackResult("b", "Subset-sum coincidence across L2's date window", "FALSE_MATCH", f"btxn_b1 wrongly asserted to {sb_matches[0].left_id}")


def attack_c_duplicate_payment() -> AttackResult:
    """Two payments against one order, identical gross, only one settled. The
    unsettled duplicate must never be matched to any settlement."""
    order = OrderRow("order_c1", "2026-08-01", "cust_1", 50_000_00, "INR", "upi", "paid", "INV-c1")
    p_settled = PaymentRow("pay_c1_settled", "order_c1", "2026-08-01T10:00:00", "upi", False, 50_000_00, 0, 0, 50_000_00, "captured", "setl_c1", None, 0)
    p_dup = PaymentRow("pay_c1_dup", "order_c1", "2026-08-01T10:05:00", "upi", False, 50_000_00, 0, 0, 50_000_00, "captured", None, None, 0)
    settlement = SettlementRow("setl_c1", "2026-08-03", "HDFCN00000000201", 1, 50_000_00, 0, 0, 0, 50_000_00)
    bank = BankRow("btxn_c1", "2026-08-03", "NEFT-HDFCN00000000201-RAZORPAY SOFTWARE PVT LTD", 50_000_00, 0, 0)
    dataset = Dataset(orders=[order], payments=[p_settled, p_dup], settlements=[settlement], bank=[bank])

    output = run_full(dataset, client=None)
    dup_has_settlement_link = any(m.link_type == "payment_settlement" and m.left_id == "pay_c1_dup" for m in output.matches)
    dup_flagged = any(e.category == "DUPLICATE_PAYMENT" and e.affected.get("payment_id") == "pay_c1_dup" for e in output.exceptions)
    if dup_has_settlement_link:
        return AttackResult("c", "Duplicate payment (L0/L1)", "FALSE_MATCH", "pay_c1_dup wrongly linked to a settlement")
    if dup_flagged:
        return AttackResult("c", "Duplicate payment (L0/L1)", "CORRECT", "pay_c1_dup correctly flagged DUPLICATE_PAYMENT, no settlement link")
    return AttackResult("c", "Duplicate payment (L0/L1)", "REFUSED", "pay_c1_dup got no settlement link (not flagged as duplicate, but not falsely matched either)")


def attack_d_refund_driven_amount_collision() -> AttackResult:
    """A refund reduces one settlement's net down to coincidentally equal a
    second, unrelated settlement's net. Both dated the same day; the
    refunded settlement's own bank credit has no recoverable UTR, forcing L1
    tolerance matching to see two equally-valid candidates."""
    o1, p1 = _order_and_payment("order_d1", "pay_d1", 50_000_00, "setl_d1")
    o2, p2 = _order_and_payment("order_d2", "pay_d2", 48_000_00, "setl_d2")
    s1 = SettlementRow("setl_d1", "2026-08-03", "HDFCN00000000301", 1, 50_000_00, 0, 0, 0, 48_000_00)  # refund-reduced net
    s2 = SettlementRow("setl_d2", "2026-08-03", "HDFCN00000000302", 1, 48_000_00, 0, 0, 0, 48_000_00)  # coincidentally same net
    bank1 = BankRow("btxn_d1", "2026-08-03", "NEFT-RAZORPAY SOFTWARE PVT LTD SETTLEMENT", 48_000_00, 0, 0)  # setl_d1's own credit, no UTR
    bank2 = BankRow("btxn_d2", "2026-08-03", "NEFT-HDFCN00000000302-RAZORPAY SOFTWARE PVT LTD", 48_000_00, 0, 0)  # setl_d2's own credit, UTR-matched
    dataset = Dataset(orders=[o1, o2], payments=[p1, p2], settlements=[s1, s2], bank=[bank1, bank2])

    output = run_full(dataset, client=None)
    btxn_d1_matches = [m for m in output.matches if m.right_id == "btxn_d1" and m.link_type == "settlement_bank_txn"]
    if not btxn_d1_matches:
        return AttackResult("d", "Refund-driven amount collision (L1)", "REFUSED", "btxn_d1 not matched to either settlement")
    if btxn_d1_matches[0].left_id == "setl_d1":
        return AttackResult("d", "Refund-driven amount collision (L1)", "CORRECT", "btxn_d1 correctly matched to its own settlement")
    return AttackResult("d", "Refund-driven amount collision (L1)", "FALSE_MATCH", f"btxn_d1 wrongly matched to {btxn_d1_matches[0].left_id}")


def attack_e_fee_adjusted_net_collision() -> AttackResult:
    """A card settlement's fee-adjusted net coincidentally equals a different,
    zero-fee settlement's gross. Both settlements' own credits are correctly
    UTR-matched; a third, unrelated stray credit (mangled UTR) happens to
    carry that same coincidental amount and must never be guessed onto
    either real settlement."""
    gross_card = 51_000_00
    fee, gst, net_card = compute_expected_fee(gross_card, "card", False)
    target = net_card
    o1, p1 = _order_and_payment("order_e1", "pay_e1", gross_card, "setl_e1", method="card")
    p1 = PaymentRow("pay_e1", "order_e1", "2026-08-01T10:00:00", "card", False, gross_card, fee, gst, net_card, "captured", "setl_e1", None, 0)
    o2, p2 = _order_and_payment("order_e2", "pay_e2", target, "setl_e2")
    s1 = SettlementRow("setl_e1", "2026-08-05", "HDFCN00000000401", 1, gross_card, fee, gst, 0, net_card)
    s2 = SettlementRow("setl_e2", "2026-08-05", "HDFCN00000000402", 1, target, 0, 0, 0, target)
    bank1 = BankRow("btxn_e1", "2026-08-05", "NEFT-HDFCN00000000401-RAZORPAY SOFTWARE PVT LTD", net_card, 0, 0)
    bank2 = BankRow("btxn_e2", "2026-08-05", "NEFT-HDFCN00000000402-RAZORPAY SOFTWARE PVT LTD", target, 0, 0)
    stray = BankRow("btxn_e3", "2026-08-05", "NEFT-RAZORPAY SOFTWARE PVT LTD SETTLEMENT", target, 0, 0)
    dataset = Dataset(orders=[o1, o2], payments=[p1, p2], settlements=[s1, s2], bank=[bank1, bank2, stray])

    output = run_full(dataset, client=None)
    stray_matches = [m for m in output.matches if m.right_id == "btxn_e3" and m.link_type == "settlement_bank_txn"]
    if not stray_matches:
        return AttackResult("e", "Fee-adjusted net collision (L1)", "REFUSED", "btxn_e3 not matched to either real settlement")
    return AttackResult("e", "Fee-adjusted net collision (L1)", "FALSE_MATCH", f"btxn_e3 wrongly matched to {stray_matches[0].left_id}")


def attack_f_settlement_double_claim() -> AttackResult:
    """The identical UTR text appears in two separate bank rows. A settlement
    is paid out exactly once - claiming it against both would double-count
    its cash. This is the attack that found a real bug (see
    engine/pipeline.py::_reconcile_settlement_credit_sums and
    ARCHITECTURE.md); also covered directly in tests/test_pipeline_run_full.py."""
    order = OrderRow("order_f1", "2026-08-01", "cust_1", 70_000_00, "INR", "upi", "paid", "INV-f1")
    payment = PaymentRow("pay_f1", "order_f1", "2026-08-01T10:00:00", "upi", False, 70_000_00, 0, 0, 70_000_00, "captured", "setl_f1", None, 0)
    settlement = SettlementRow("setl_f1", "2026-08-03", "HDFCN00000000501", 1, 70_000_00, 0, 0, 0, 70_000_00)
    genuine = BankRow("btxn_f1", "2026-08-03", "NEFT-HDFCN00000000501-RAZORPAY SOFTWARE PVT LTD", 70_000_00, 0, 0)
    collision = BankRow("btxn_f2", "2026-08-03", "NEFT-HDFCN00000000501-RAZORPAY SOFTWARE PVT LTD", 70_000_00, 0, 0)
    dataset = Dataset(orders=[order], payments=[payment], settlements=[settlement], bank=[genuine, collision])

    output = run_full(dataset, client=None)
    sb_matches = [m for m in output.matches if m.link_type == "settlement_bank_txn" and m.left_id == "setl_f1"]
    if len(sb_matches) >= 2:
        return AttackResult("f", "Settlement double-claim across two bank rows sharing a UTR", "FALSE_MATCH", "setl_f1 claimed against both bank rows")
    return AttackResult("f", "Settlement double-claim across two bank rows sharing a UTR", "REFUSED", f"setl_f1 claimed against at most one row ({len(sb_matches)})")


def attack_g_l2_ambiguity() -> AttackResult:
    """Three settlements where two different subsets both sum to the bank
    credit exactly - {s1} and {s2, s3} both equal the credit. L2's ambiguity
    guard must refuse both subsets, not pick either."""
    o1, p1 = _order_and_payment("order_g1", "pay_g1", 500_000_00, "setl_g1")
    o2, p2 = _order_and_payment("order_g2", "pay_g2", 200_000_00, "setl_g2")
    o3, p3 = _order_and_payment("order_g3", "pay_g3", 300_000_00, "setl_g3")
    s1 = SettlementRow("setl_g1", "2026-08-02", "HDFCN00000000701", 1, 500_000_00, 0, 0, 0, 500_000_00)
    s2 = SettlementRow("setl_g2", "2026-08-02", "HDFCN00000000702", 1, 200_000_00, 0, 0, 0, 200_000_00)
    s3 = SettlementRow("setl_g3", "2026-08-02", "HDFCN00000000703", 1, 300_000_00, 0, 0, 0, 300_000_00)
    bank = BankRow("btxn_g1", "2026-08-02", "NEFT-UNKNOWN REF-RAZORPAY SOFTWARE PVT LTD", 500_000_00, 0, 0)
    dataset = Dataset(orders=[o1, o2, o3], payments=[p1, p2, p3], settlements=[s1, s2, s3], bank=[bank])

    output = run_full(dataset, client=None)
    sb_matches = [m for m in output.matches if m.link_type == "settlement_bank_txn" and m.right_id == "btxn_g1"]
    if not sb_matches:
        return AttackResult("g", "Two subsets both sum to the credit (L2 ambiguity guard)", "REFUSED", "btxn_g1 not matched to any subset")
    matched_ids = sorted(m.left_id for m in sb_matches)
    return AttackResult("g", "Two subsets both sum to the credit (L2 ambiguity guard)", "FALSE_MATCH", f"btxn_g1 wrongly matched to {matched_ids}")


ALL_ATTACKS = [
    attack_a_rekeyed_utr_prefix,
    attack_b_subset_sum_across_date_window,
    attack_c_duplicate_payment,
    attack_d_refund_driven_amount_collision,
    attack_e_fee_adjusted_net_collision,
    attack_f_settlement_double_claim,
    attack_g_l2_ambiguity,
]


def run_all_attacks() -> list[AttackResult]:
    """Runs every attack and returns its outcome. Any FALSE_MATCH here is a
    real bug - report it and fix the engine, never adjust the attack to make
    it pass. (Attack f originally found one this way - see
    engine/pipeline.py::_reconcile_settlement_credit_sums.)"""
    return [attack() for attack in ALL_ATTACKS]
