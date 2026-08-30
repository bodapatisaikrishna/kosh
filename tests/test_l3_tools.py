"""engine/l3_tools.py: the structural enforcement of L3's hard constraints. The
prompt asks nicely; these tests are what actually prove a violation gets
rejected rather than silently accepted.
"""

from __future__ import annotations

from engine.fees import compute_expected_fee
from engine.io import BankRow, Dataset, OrderRow, PaymentRow, SettlementRow
from engine.l3_tools import (
    ToolContext,
    ToolError,
    dispatch_tool,
    find_candidates,
    get_record,
    propose_match,
    raise_exception,
    solve_subset_tool,
)


def _dataset():
    order = OrderRow("order_1", "2026-08-01", "cust_1", 100_000_00, "INR", "upi", "paid", "INV-1")
    fee, gst, net = compute_expected_fee(100_000_00, "upi", False)
    payment = PaymentRow(
        "pay_1", "order_1", "2026-08-01T10:00:00", "upi", False, 100_000_00, fee, gst, net,
        "captured", None, None, 0,
    )
    settlement = SettlementRow("setl_1", "2026-08-03", "HDFCN00000000001", 1, 100_000_00, fee, gst, 0, net)
    bank = BankRow("btxn_1", "2026-08-03", "NEFT-HDFCN00000000001-RAZORPAY SOFTWARE PVT LTD", net, 0, 0)
    return Dataset(orders=[order], payments=[payment], settlements=[settlement], bank=[bank])


def _ctx(residual_id="pay_1", residual_source="payments"):
    return ToolContext(dataset=_dataset(), residual_id=residual_id, residual_source=residual_source)


# --- get_record ---------------------------------------------------------------

def test_get_record_returns_the_row_and_registers_the_id():
    ctx = _ctx()
    record = get_record(ctx, "settlements", "setl_1")
    assert record["settlement_id"] == "setl_1"
    assert "setl_1" in ctx.known_ids


def test_get_record_rejects_an_unknown_id_never_fabricates():
    ctx = _ctx()
    try:
        get_record(ctx, "settlements", "setl_does_not_exist")
        assert False, "expected ToolError"
    except ToolError:
        pass


def test_get_record_rejects_an_unknown_source():
    ctx = _ctx()
    try:
        get_record(ctx, "not_a_table", "pay_1")
        assert False, "expected ToolError"
    except ToolError:
        pass


# --- find_candidates -----------------------------------------------------------

def test_find_candidates_requires_a_previously_known_id():
    ctx = _ctx()
    try:
        find_candidates(ctx, "setl_1", amount_tolerance_paise=100, date_window_days=5)
        assert False, "expected ToolError"
    except ToolError:
        pass


def test_find_candidates_from_a_payment_finds_the_matching_settlement():
    ctx = _ctx()  # residual_id="pay_1" is auto-registered as known
    candidates = find_candidates(ctx, "pay_1", amount_tolerance_paise=100, date_window_days=5)
    assert len(candidates) == 1
    assert candidates[0]["settlement_id"] == "setl_1"
    assert "setl_1" in ctx.known_ids


def test_find_candidates_registers_returned_ids_for_later_use():
    ctx = _ctx()
    find_candidates(ctx, "pay_1", amount_tolerance_paise=100, date_window_days=5)
    assert "setl_1" in ctx.known_ids


# --- solve_subset --------------------------------------------------------------

def test_solve_subset_rejects_unknown_candidate_ids():
    ctx = _ctx()
    try:
        solve_subset_tool(ctx, target_paise=100_00, candidate_ids=["setl_never_seen"])
        assert False, "expected ToolError"
    except ToolError:
        pass


def test_solve_subset_solves_with_known_candidates():
    ctx = _ctx()
    get_record(ctx, "settlements", "setl_1")
    result = solve_subset_tool(ctx, target_paise=ctx.dataset.settlements[0].net_paise, candidate_ids=["setl_1"], tolerance_paise=0)
    assert result["status"] == "SOLVED"
    assert result["chosen_ids"] == ["setl_1"]


# --- propose_match --------------------------------------------------------------

def test_propose_match_rejects_an_unknown_record_id():
    ctx = _ctx()
    try:
        propose_match(ctx, record_ids=["pay_1", "setl_never_seen"], confidence=0.95, rationale="setl_never_seen matches pay_1")
        assert False, "expected ToolError"
    except ToolError:
        pass


def test_propose_match_rejects_low_confidence():
    ctx = _ctx()
    get_record(ctx, "settlements", "setl_1")
    try:
        propose_match(ctx, record_ids=["pay_1", "setl_1"], confidence=0.5, rationale="setl_1 looks close to pay_1")
        assert False, "expected ToolError"
    except ToolError:
        pass


def test_propose_match_rejects_a_rationale_that_cites_nothing_known():
    ctx = _ctx()
    get_record(ctx, "settlements", "setl_1")
    try:
        propose_match(ctx, record_ids=["pay_1", "setl_1"], confidence=0.95, rationale="I am confident this is correct")
        assert False, "expected ToolError"
    except ToolError:
        pass


def test_propose_match_succeeds_and_infers_the_link_type():
    ctx = _ctx()
    get_record(ctx, "settlements", "setl_1")
    result = propose_match(ctx, record_ids=["pay_1", "setl_1"], confidence=0.95, rationale="setl_1 matches pay_1 exactly")
    assert result["status"] == "ok"
    assert len(result["matches"]) == 1
    assert result["matches"][0]["link_type"] == "payment_settlement"
    assert result["matches"][0]["left_id"] == "pay_1"
    assert result["matches"][0]["right_id"] == "setl_1"
    assert ctx.result[0] == "match"


def test_propose_match_infers_settlement_bank_link_type():
    ctx = _ctx(residual_id="btxn_1", residual_source="bank")
    get_record(ctx, "settlements", "setl_1")
    result = propose_match(ctx, record_ids=["btxn_1", "setl_1"], confidence=0.95, rationale="setl_1 matches btxn_1")
    assert result["matches"][0]["link_type"] == "settlement_bank_txn"
    assert result["matches"][0]["left_id"] == "setl_1"
    assert result["matches"][0]["right_id"] == "btxn_1"


def test_propose_match_over_50000_gets_a_high_value_companion_exception():
    ctx = _ctx()
    get_record(ctx, "settlements", "setl_1")
    result = propose_match(ctx, record_ids=["pay_1", "setl_1"], confidence=0.95, rationale="setl_1 matches pay_1")
    assert result["companion_exception"] is not None
    assert result["companion_exception"]["category"] == "HIGH_VALUE_MATCH_REVIEW"
    assert result["companion_exception"]["severity"] == "REVIEW_REQUIRED"


def test_propose_match_after_a_decision_is_rejected():
    ctx = _ctx()
    get_record(ctx, "settlements", "setl_1")
    propose_match(ctx, record_ids=["pay_1", "setl_1"], confidence=0.95, rationale="setl_1 matches pay_1")
    try:
        propose_match(ctx, record_ids=["pay_1", "setl_1"], confidence=0.95, rationale="setl_1 matches pay_1 again")
        assert False, "expected ToolError"
    except ToolError:
        pass


# --- raise_exception ------------------------------------------------------------

def test_raise_exception_ignores_the_models_severity_and_recomputes_it():
    ctx = _ctx()
    result = raise_exception(
        ctx, category="UNEXPLAINED_VARIANCE", severity="STANDARD", amount_at_risk_paise=60_000_00,
        recommended_action="Investigate manually", rationale="pay_1's net does not match any known cause",
    )
    assert result["exception"]["severity"] == "REVIEW_REQUIRED"  # amount > 50,000 forces this, regardless of the model's input


def test_raise_exception_rejects_an_empty_rationale():
    ctx = _ctx()
    try:
        raise_exception(ctx, category="UNEXPLAINED_VARIANCE", severity="STANDARD", amount_at_risk_paise=100_00, recommended_action="x", rationale="")
        assert False, "expected ToolError"
    except ToolError:
        pass


# --- dispatch_tool --------------------------------------------------------------

def test_dispatch_tool_reports_an_unknown_tool_name_as_a_result_not_an_exception():
    ctx = _ctx()
    result = dispatch_tool(ctx, "not_a_real_tool", {})
    assert result["status"] == "error"


def test_dispatch_tool_converts_tool_error_into_an_error_result():
    ctx = _ctx()
    result = dispatch_tool(ctx, "get_record", {"source": "payments", "record_id": "pay_does_not_exist"})
    assert result["status"] == "error"


def test_dispatch_tool_returns_ok_for_a_successful_call():
    ctx = _ctx()
    result = dispatch_tool(ctx, "get_record", {"source": "settlements", "record_id": "setl_1"})
    assert result["status"] == "ok"


# --- regression: a payment must never link directly to a CREDIT bank row -----
# Caught in a real NIM run: the model correctly traced payment -> settlement ->
# the settlement's own bank credit, bundled all three ids into one
# propose_match call, and the tool mislabeled the third leg as a
# chargeback_payment link (which only makes sense for a debit) - a genuine
# false match. See engine/l3_agent.py's synthetic exercise notes.

def test_propose_match_rejects_a_payment_linked_directly_to_a_settlement_credit():
    dataset = _dataset()  # bank[0] is a CREDIT row (net_paise, debit_paise=0)
    ctx = _ctx(residual_id="pay_1", residual_source="payments")
    get_record(ctx, "settlements", "setl_1")
    get_record(ctx, "bank", "btxn_1")
    try:
        propose_match(
            ctx, record_ids=["pay_1", "setl_1", "btxn_1"], confidence=0.98,
            rationale="setl_1 matches pay_1 and btxn_1 is setl_1's own bank credit",
        )
        assert False, "expected ToolError - a payment must not link directly to a settlement's own credit"
    except ToolError:
        pass
    assert ctx.result is None  # the whole call is rejected, not partially applied


def test_propose_match_still_allows_a_genuine_chargeback_debit_link():
    dataset = _dataset()
    ctx = _ctx(residual_id="pay_1", residual_source="payments")
    debit = BankRow("btxn_chargeback", "2026-08-05", "CHARGEBACK DR-ref-RAZORPAY SOFTWARE", 0, 500_00, 0)
    ctx.dataset.bank.append(debit)
    ctx.known_ids.add("btxn_chargeback")
    result = propose_match(
        ctx, record_ids=["pay_1", "btxn_chargeback"], confidence=0.95,
        rationale="btxn_chargeback is a genuine chargeback debit against pay_1",
    )
    assert result["status"] == "ok"
    assert result["matches"][0]["link_type"] == "chargeback_payment"
