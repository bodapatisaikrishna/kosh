"""eval/metrics.py's compute_defect_confusion, specifically for exceptions
raised by L3 (engine/l3_tools.py's raise_exception), not just the
deterministic classifier's own convention.

Regression coverage for a real bug: raise_exception used to stamp affected
as {"record_id": ..., "source": ...} only. CATEGORY_IDENTITY_FIELDS for most
categories (FEE_VARIANCE, TAX_VARIANCE, ...) looks for the source-specific
field ("payment_id"), which was never present - every non-UNEXPLAINED_VARIANCE
category an agent raised silently vanished from per-defect-class scoring,
counted as "missed" even though a correct exception had been raised.
"""

from __future__ import annotations

from engine.contract import EngineOutput
from engine.l3_tools import ToolContext, raise_exception
from eval.metrics import compute_defect_confusion
from tests.test_l3_tools import _dataset


def _ground_truth_with_one_defect(dtype: str, category: str, payment_id: str = "pay_1") -> dict:
    return {"defects": [{
        "type": dtype,
        "affected": {"payment_id": payment_id},
        "expected_exception_category": category,
        "amount_at_risk_paise": 500,
        "resolvable_by_engine": False,
    }]}


def test_l3_fee_variance_exception_is_detected_not_missed():
    ctx = ToolContext(dataset=_dataset(), residual_id="pay_1", residual_source="payments")
    raise_exception(
        ctx, category="FEE_VARIANCE", severity="STANDARD", amount_at_risk_paise=500,
        recommended_action="x", rationale="a real reason",
    )
    exc = ctx.result[1]["exception"]

    gt = _ground_truth_with_one_defect("compound_fee_tax_error", "FEE_VARIANCE")
    confusion = compute_defect_confusion(EngineOutput(matches=[], exceptions=[exc]), gt)
    assert confusion["compound_fee_tax_error"] == {"detected": 1}


def test_l3_agent_incomplete_is_misclassified_not_missed():
    ctx = ToolContext(dataset=_dataset(), residual_id="pay_1", residual_source="payments")
    raise_exception(
        ctx, category="AGENT_INCOMPLETE", severity="STANDARD", amount_at_risk_paise=500,
        recommended_action="x", rationale="turn budget exhausted",
    )
    exc = ctx.result[1]["exception"]

    # The underlying defect's true category is FEE_VARIANCE, but the agent
    # ran out of turns - this should read as "we raised something, just not
    # the right label", not "we raised nothing at all".
    gt = _ground_truth_with_one_defect("compound_fee_tax_error", "FEE_VARIANCE")
    confusion = compute_defect_confusion(EngineOutput(matches=[], exceptions=[exc]), gt)
    assert confusion["compound_fee_tax_error"] == {"misclassified": 1}
