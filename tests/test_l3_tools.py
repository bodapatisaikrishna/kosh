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
    VarianceObservation,
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


def test_find_candidates_on_a_never_captured_payment_raises_not_crashes():
    # A "failed" payment has no captured_at at all - found by the all-LLM
    # ablation (Task 4), the first pipeline mode that ever routes a failed
    # payment to L3 (run_full never does - only captured payments with a
    # genuine reconciliation question reach L3 there). Must be a structural
    # ToolError, not a raw ValueError crashing the whole batch.
    dataset = _dataset()
    failed_payment = PaymentRow("pay_failed", "order_1", "", "upi", False, 100_000_00, 0, 0, 0, "failed", None, None, 0)
    dataset.payments.append(failed_payment)
    ctx = ToolContext(dataset=dataset, residual_id="pay_failed", residual_source="payments")
    try:
        find_candidates(ctx, "pay_failed", amount_tolerance_paise=100, date_window_days=5)
        assert False, "expected ToolError"
    except ToolError as exc:
        assert "captured_at" in str(exc)


def test_find_candidates_on_a_bank_debit_skips_never_captured_payments():
    # A never-captured payment must never be considered a chargeback
    # candidate for a bank debit - and must not crash the search either.
    dataset = _dataset()
    failed_payment = PaymentRow("pay_failed", "order_1", "", "upi", False, 500_00, 0, 0, 500_00, "failed", None, None, 0)
    dataset.payments.append(failed_payment)
    debit = BankRow("btxn_debit", "2026-08-04", "CHARGEBACK DEBIT", 0, 500_00, 0)
    dataset.bank.append(debit)
    ctx = ToolContext(dataset=dataset, residual_id="btxn_debit", residual_source="bank")
    candidates = find_candidates(ctx, "btxn_debit", amount_tolerance_paise=100, date_window_days=5)
    assert all(c.get("payment_id") != "pay_failed" for c in candidates)


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


def test_raise_exception_rejects_an_invented_category():
    # A live run once returned "FEE_CALCULATION_VARIANCE" and
    # "unexplained_fee_variance" - neither a real category. Both would have
    # silently broken category-keyed scoring downstream (compute_defect_confusion's
    # exact-match check, the dashboard's by-category breakdown) with no error
    # anywhere. This must be rejected structurally, not just discouraged in the
    # prompt - same principle as every other hard constraint in this file.
    ctx = _ctx()
    try:
        raise_exception(
            ctx, category="FEE_CALCULATION_VARIANCE", severity="STANDARD",
            amount_at_risk_paise=100_00, recommended_action="x", rationale="a real reason",
        )
        assert False, "expected ToolError"
    except ToolError as exc:
        assert "FEE_CALCULATION_VARIANCE" in str(exc)
        assert "UNEXPLAINED_VARIANCE" in str(exc)  # a real category is listed as guidance
    assert ctx.result is None  # the rejected call must not have closed the loop


def test_raise_exception_affected_carries_the_source_specific_id_field():
    # Without "payment_id" (not just "record_id"), eval/metrics.py's
    # CATEGORY_IDENTITY_FIELDS lookup for FEE_VARIANCE/TAX_VARIANCE/etc never
    # finds a match against this exception - it silently vanishes from
    # per-defect-class scoring even though it was correctly raised.
    ctx = _ctx(residual_id="pay_1", residual_source="payments")
    result = raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=500,
        recommended_action="x", rationale="a real reason",
    )
    affected = result["exception"]["affected"]
    assert affected["payment_id"] == "pay_1"
    assert affected["record_id"] == "pay_1"  # generic key still present too


def test_raise_exception_affected_uses_bank_txn_id_for_bank_source():
    ctx = _ctx(residual_id="btxn_1", residual_source="bank")
    result = raise_exception(
        ctx, category="UNIDENTIFIED_CREDIT", severity="STANDARD", amount_at_risk_paise=500,
        recommended_action="x", rationale="a real reason",
    )
    assert result["exception"]["affected"]["bank_txn_id"] == "btxn_1"


def test_raise_exception_computes_real_aging_days_not_zero():
    # aging_days must never be a hardcoded 0 for an L3-raised exception either -
    # same bug class as the deterministic classifier's, same fix.
    order = OrderRow("order_1", "2026-08-01", "cust_1", 100_000_00, "INR", "upi", "paid", "INV-1")
    fee, gst, net = compute_expected_fee(100_000_00, "upi", False)
    old_payment = PaymentRow("pay_old", "order_1", "2026-08-01T10:00:00", "upi", False, 100_000_00, fee, gst, net, "captured", None, None, 0)
    new_payment = PaymentRow("pay_new", "order_1", "2026-08-15T10:00:00", "upi", False, 100_000_00, fee, gst, net, "captured", None, None, 0)
    dataset = Dataset(orders=[order], payments=[old_payment, new_payment], settlements=[], bank=[])
    ctx = ToolContext(dataset=dataset, residual_id="pay_old", residual_source="payments")

    result = raise_exception(
        ctx, category="UNEXPLAINED_VARIANCE", severity="STANDARD", amount_at_risk_paise=500,
        recommended_action="x", rationale="a real reason",
    )
    # "now" is inferred as the latest capture in the dataset (Aug 15, from
    # pay_new); pay_old was captured Aug 1 - 14 days old.
    assert result["exception"]["aging_days"] == 14


def test_raise_exception_on_a_never_captured_payment_gets_zero_aging_not_a_crash():
    # A "failed" payment has no captured_at at all - aging_days must degrade
    # to 0 (unknown), same as when the record itself can't be found, never
    # crash. Found by the all-LLM ablation (Task 4).
    order = OrderRow("order_1", "2026-08-01", "cust_1", 100_000_00, "INR", "upi", "paid", "INV-1")
    failed_payment = PaymentRow("pay_failed", "order_1", "", "upi", False, 100_000_00, 0, 0, 0, "failed", None, None, 0)
    dataset = Dataset(orders=[order], payments=[failed_payment], settlements=[], bank=[])
    ctx = ToolContext(dataset=dataset, residual_id="pay_failed", residual_source="payments")

    result = raise_exception(
        ctx, category="UNEXPLAINED_VARIANCE", severity="STANDARD", amount_at_risk_paise=500,
        recommended_action="x", rationale="a real reason",
    )
    assert result["exception"]["aging_days"] == 0


def test_raise_exception_uses_a_real_differentiated_owner():
    ctx = _ctx()
    result = raise_exception(
        ctx, category="FX_VARIANCE", severity="STANDARD", amount_at_risk_paise=500,
        recommended_action="x", rationale="a real reason",
    )
    assert result["exception"]["suggested_owner"] == "Treasury"


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
    # _ctx()'s dataset has btxn_1 as a CREDIT row (credit=net_paise, debit=0).
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


# --- raise_exception: compound-leg coercion (Task 2) --------------------------
#
# The structural fix: if the model's own tool-call history shows 2+ distinct
# simultaneously-undecomposable comparisons, raise_exception recomputes the
# category (to UNEXPLAINED_VARIANCE) and the amount (to the bottom-line net
# delta, not whichever leg had the biggest delta) - the same enforcement
# pattern as severity_for_amount, triggered by tool-call history, not prompting.

def test_single_explained_leg_no_coercion():
    ctx = _ctx()
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=1174, expected_paise=1000, delta_paise=174, cause="FEE_TIER")
    )
    result = raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=174,
        recommended_action="x", rationale="wrong fee tier applied",
    )
    assert result["category_coerced_from"] is None
    assert result["unexplained_leg_count"] == 0
    assert result["exception"]["category"] == "FEE_VARIANCE"
    assert result["exception"]["amount_at_risk_paise"] == 174


def test_single_unexplained_leg_below_threshold_no_coercion():
    # The false-positive guard: over-firing here would turn every genuine
    # single-cause FEE_VARIANCE exception into a vague UNEXPLAINED_VARIANCE
    # one and lose per-class accuracy on a type that currently scores
    # perfectly. One unexplained leg alone must never coerce.
    ctx = _ctx()
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=57457, expected_paise=57315, delta_paise=142, cause="UNEXPLAINED")
    )
    result = raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=142,
        recommended_action="x", rationale="fee looks off, cause unclear",
    )
    assert result["category_coerced_from"] is None
    assert result["unexplained_leg_count"] == 1
    assert result["exception"]["category"] == "FEE_VARIANCE"


def test_compound_fee_and_tax_coerces():
    ctx = _ctx()
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=1115, expected_paise=1174, delta_paise=-59, cause="UNEXPLAINED")
    )
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=128, expected_paise=211, delta_paise=-83, cause="UNEXPLAINED")
    )
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=57457, expected_paise=57315, delta_paise=142, cause="UNEXPLAINED")
    )
    result = raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=59,
        recommended_action="x", rationale="fee looks wrong",
    )
    assert result["category_coerced_from"] == "FEE_VARIANCE"
    assert result["unexplained_leg_count"] == 3
    assert result["exception"]["category"] == "UNEXPLAINED_VARIANCE"


def test_retried_identical_comparison_does_not_inflate():
    # A model that re-checks itself with the exact same explain_variance call
    # twice (legitimate - it's double-checking) must not have that count as
    # two distinct legs.
    ctx = _ctx()
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=57457, expected_paise=57315, delta_paise=142, cause="UNEXPLAINED")
    )
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=57457, expected_paise=57315, delta_paise=142, cause="UNEXPLAINED")
    )
    result = raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=142,
        recommended_action="x", rationale="fee looks off, double-checked",
    )
    assert result["unexplained_leg_count"] == 1  # deduped, not 2
    assert result["category_coerced_from"] is None


def test_zero_delta_never_counts():
    ctx = _ctx()
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=1000, expected_paise=1000, delta_paise=0, cause="UNEXPLAINED")
    )
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=57457, expected_paise=57315, delta_paise=142, cause="UNEXPLAINED")
    )
    result = raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=142,
        recommended_action="x", rationale="fee looks off",
    )
    assert result["unexplained_leg_count"] == 1  # the zero-delta leg is ignored
    assert result["category_coerced_from"] is None


def test_bottom_line_picks_net_not_largest_delta():
    # The real bug this fix closes: a live run reported amount_at_risk_paise
    # 744 (the fee leg's own delta) for a defect ground truth labels 183
    # paise (the true net delta), because the two legs partially offset.
    ctx = _ctx()
    ctx.variance_observations.append(
        # fee leg: small expected amount, large delta
        VarianceObservation(observed_paise=430, expected_paise=1174, delta_paise=744, cause="UNEXPLAINED")
    )
    ctx.variance_observations.append(
        # net leg: large expected amount (the bottom line), smaller delta
        VarianceObservation(observed_paise=57498, expected_paise=57315, delta_paise=183, cause="UNEXPLAINED")
    )
    result = raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=744,
        recommended_action="x", rationale="fee looks very wrong",
    )
    assert result["exception"]["amount_at_risk_paise"] == 183  # net delta, not the 744 leg delta
    assert result["exception"]["category"] == "UNEXPLAINED_VARIANCE"


def test_coercion_appears_in_evidence_chain():
    ctx = _ctx()
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=430, expected_paise=1174, delta_paise=744, cause="UNEXPLAINED")
    )
    ctx.variance_observations.append(
        VarianceObservation(observed_paise=57498, expected_paise=57315, delta_paise=183, cause="UNEXPLAINED")
    )
    result = raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=744,
        recommended_action="x", rationale="fee looks very wrong",
    )
    evidence = result["exception"]["evidence_chain"]
    assert evidence[0] == "fee looks very wrong"  # original rationale kept, not dropped
    assert any("coerced from FEE_VARIANCE to UNEXPLAINED_VARIANCE" in e for e in evidence)
    assert any("744" in e and "unexplained leg" in e for e in evidence)
    assert any("183" in e and "bottom line" in e for e in evidence)
