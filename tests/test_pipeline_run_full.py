"""Phase 5 checkpoint: engine/pipeline.py::run_full, the complete pipeline.

    auto-match rate >= 96%
    false_match_rate <= 0.1% (ideally still 0.00%)
    every exception has a non-empty recommended_action
    precision/recall per defect class reported

Also covers the two residual-handling paths directly: with a client, a
genuinely unexplained payment reaches L3; without one, it still never
silently vanishes - it becomes an honest UNEXPLAINED_VARIANCE fallback
exception instead.
"""

from __future__ import annotations

from pathlib import Path

from engine.fees import compute_expected_fee
from engine.io import BankRow, Dataset, OrderRow, PaymentRow, SettlementRow, load_dataset
from engine.llm.base import AssistantTurn, ToolCall
from engine.llm.fake_client import FakeClient
from engine.pipeline import run_full
from eval.io import load_ground_truth
from eval.metrics import compute_metrics

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "run_2000"


def test_phase5_checkpoint_on_run_2000():
    dataset = load_dataset(FIXTURES)
    ground_truth = load_ground_truth(FIXTURES)

    output = run_full(dataset, client=None)
    metrics = compute_metrics(dataset, output, ground_truth)

    assert metrics["accuracy"]["auto_match_rate"] >= 0.96
    assert metrics["accuracy"]["false_match_rate"] <= 0.001
    assert all(e.recommended_action for e in output.exceptions)
    assert metrics["accuracy"]["defect_confusion"]  # per-defect-class breakdown is present


def _dataset_with_unexplained_payment():
    order = OrderRow("order_1", "2026-08-01", "cust_1", 100_000_00, "INR", "upi", "paid", "INV-1")
    fee, gst, _correct_net = compute_expected_fee(100_000_00, "upi", False)
    weird_net = 100_000_00 - fee - gst - 12_345  # doesn't fit any known deterministic hypothesis
    payment = PaymentRow("pay_1", "order_1", "2026-08-01T10:00:00", "upi", False, 100_000_00, fee, gst, weird_net, "captured", "setl_1", None, 0)
    settlement = SettlementRow("setl_1", "2026-08-03", "HDFCN00000000001", 1, 100_000_00, fee, gst, 0, weird_net)
    bank = BankRow("btxn_1", "2026-08-03", "NEFT-HDFCN00000000001-RAZORPAY SOFTWARE PVT LTD", weird_net, 0, 0)
    return Dataset(orders=[order], payments=[payment], settlements=[settlement], bank=[bank])


def test_unexplained_payment_reaches_l3_when_a_client_is_given(tmp_path):
    client = FakeClient([
        AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
            "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 12_345,
            "recommended_action": "investigate manually", "rationale": "pay_1's net does not fit any known hypothesis",
        }),), stop_reason="tool_use"),
    ])
    output = run_full(_dataset_with_unexplained_payment(), client=client, model_name="fake", backend_name="fake",
                       cache_dir=tmp_path / "cache", traces_dir=tmp_path / "traces")
    assert output.meta.llm_calls == 1
    assert any(e.category == "UNEXPLAINED_VARIANCE" and e.affected.get("record_id") == "pay_1" for e in output.exceptions)


def test_unexplained_payment_gets_a_fallback_exception_without_a_client():
    output = run_full(_dataset_with_unexplained_payment(), client=None)
    assert output.meta.llm_calls == 0
    matching = [e for e in output.exceptions if e.category == "UNEXPLAINED_VARIANCE" and e.affected.get("payment_id") == "pay_1"]
    assert len(matching) == 1
    assert matching[0].recommended_action


# --- settlement credit-sum reconciliation (Task 3, attack f) -----------------
#
# A settlement's linked bank credits must never sum to more than its own
# net_paise. Two bank rows can legitimately both belong to one settlement
# (settlement_split - a genuine payout divided into two real deposits, each
# a partial amount summing exactly to net_paise); what must never happen is a
# settlement being claimed against more total credit than it was ever paid -
# found by deliberately colliding the identical UTR text on two unrelated
# bank rows.

def test_settlement_double_claim_via_identical_utr_is_refused():
    order = OrderRow("order_f1", "2026-08-01", "cust_1", 70_000_00, "INR", "upi", "paid", "INV-f1")
    payment = PaymentRow("pay_f1", "order_f1", "2026-08-01T10:00:00", "upi", False, 70_000_00, 0, 0, 70_000_00, "captured", "setl_f1", None, 0)
    settlement = SettlementRow("setl_f1", "2026-08-06", "HDFCN00000000501", 1, 70_000_00, 0, 0, 0, 70_000_00)
    genuine = BankRow("btxn_f1", "2026-08-06", "NEFT-HDFCN00000000501-RAZORPAY SOFTWARE PVT LTD", 70_000_00, 0, 0)
    collision = BankRow("btxn_f2", "2026-08-06", "NEFT-HDFCN00000000501-RAZORPAY SOFTWARE PVT LTD", 70_000_00, 0, 0)
    dataset = Dataset(orders=[order], payments=[payment], settlements=[settlement], bank=[genuine, collision])

    output = run_full(dataset, client=None)
    settlement_bank_matches = [m for m in output.matches if m.link_type == "settlement_bank_txn"]
    assert settlement_bank_matches == []  # neither row is asserted - refuse, don't guess which is real
    # both rows must still surface somewhere - never silently vanish
    unidentified = {e.affected.get("bank_txn_id") for e in output.exceptions if e.category == "UNIDENTIFIED_CREDIT"}
    assert {"btxn_f1", "btxn_f2"} <= unidentified


def test_legitimate_settlement_split_still_resolves_both_parts():
    order = OrderRow("order_sp1", "2026-08-01", "cust_1", 100_000_00, "INR", "upi", "paid", "INV-sp1")
    payment = PaymentRow("pay_sp1", "order_sp1", "2026-08-01T10:00:00", "upi", False, 100_000_00, 0, 0, 100_000_00, "captured", "setl_sp1", None, 0)
    settlement = SettlementRow("setl_sp1", "2026-08-06", "HDFCN00000000601", 2, 100_000_00, 0, 0, 0, 100_000_00)
    part_a = BankRow("btxn_sp1a", "2026-08-06", "NEFT-HDFCN00000000601-RAZORPAY SOFTWARE PVT LTD", 60_000_00, 0, 0)
    part_b = BankRow("btxn_sp1b", "2026-08-06", "NEFT-HDFCN00000000601-RAZORPAY SOFTWARE PVT LTD", 40_000_00, 0, 0)
    dataset = Dataset(orders=[order], payments=[payment], settlements=[settlement], bank=[part_a, part_b])

    output = run_full(dataset, client=None)
    settlement_bank_matches = {m.right_id for m in output.matches if m.link_type == "settlement_bank_txn"}
    assert settlement_bank_matches == {"btxn_sp1a", "btxn_sp1b"}  # both parts, still correctly resolved
    assert not any(e.category == "UNIDENTIFIED_CREDIT" for e in output.exceptions)
