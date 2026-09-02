"""eval/metrics.py::compute_fee_leakage - the industry-standard "delta between
expected and actual PSP fees" metric, aggregated from the exception ledger.

FEE_LEAKAGE_CATEGORIES is deliberately narrow (FEE_VARIANCE, TAX_VARIANCE,
FX_VARIANCE only) - these tests exist specifically to guard that narrowness:
a category creeping into the aggregate that isn't a genuine overcharge would
inflate the headline number into meaninglessness.
"""

from __future__ import annotations

from engine.contract import EngineMeta, EngineOutput, ReconException
from eval.metrics import FEE_LEAKAGE_CATEGORIES, compute_fee_leakage


def _exception(category: str, amount_paise: int) -> ReconException:
    return ReconException(
        category=category, severity="STANDARD", amount_at_risk_paise=amount_paise,
        affected={"payment_id": f"pay_{category.lower()}"}, recommended_action="x",
    )


def _output(exceptions: list[ReconException]) -> EngineOutput:
    return EngineOutput(matches=[], exceptions=exceptions, meta=EngineMeta(wall_clock_seconds=0.0))


def test_empty_exception_list_yields_zero_leakage():
    result = compute_fee_leakage(_output([]))
    assert result["total_paise"] == 0
    assert result["affected_records"] == 0
    assert result["is_lower_bound"] is True
    for cat in FEE_LEAKAGE_CATEGORIES:
        assert result["by_category"][cat] == {"count": 0, "amount_paise": 0}


def test_only_non_fee_categories_present_yields_zero_total():
    """Guards the exclusion list: MISSING_SETTLEMENT/PERIOD_CUTOFF/
    DUPLICATE_PAYMENT/UNIDENTIFIED_CREDIT/REFUND_MISALLOCATION/
    ORPHAN_CHARGEBACK are timing, duplication, or attribution problems, not
    overcharges - none of them should ever contribute to this total."""
    non_fee_categories = [
        "MISSING_SETTLEMENT", "PERIOD_CUTOFF", "DUPLICATE_PAYMENT",
        "UNIDENTIFIED_CREDIT", "REFUND_MISALLOCATION", "ORPHAN_CHARGEBACK",
    ]
    exceptions = [_exception(cat, 10_000_00) for cat in non_fee_categories]
    result = compute_fee_leakage(_output(exceptions))
    assert result["total_paise"] == 0
    assert result["affected_records"] == 0


def test_mixed_fixture_totals_the_three_fee_categories_only():
    exceptions = [
        _exception("FEE_VARIANCE", 1_000),
        _exception("FEE_VARIANCE", 2_500),
        _exception("TAX_VARIANCE", 3_000),
        _exception("FX_VARIANCE", 4_750),
        _exception("MISSING_SETTLEMENT", 999_999),  # must not be counted
        _exception("PERIOD_CUTOFF", 999_999),  # must not be counted
    ]
    result = compute_fee_leakage(_output(exceptions))

    hand_computed_total = 1_000 + 2_500 + 3_000 + 4_750
    assert result["total_paise"] == hand_computed_total
    assert result["affected_records"] == 4
    assert result["by_category"]["FEE_VARIANCE"] == {"count": 2, "amount_paise": 3_500}
    assert result["by_category"]["TAX_VARIANCE"] == {"count": 1, "amount_paise": 3_000}
    assert result["by_category"]["FX_VARIANCE"] == {"count": 1, "amount_paise": 4_750}


def test_by_category_counts_sum_to_affected_records():
    exceptions = [
        _exception("FEE_VARIANCE", 100),
        _exception("TAX_VARIANCE", 200),
        _exception("TAX_VARIANCE", 300),
        _exception("FX_VARIANCE", 400),
        _exception("FX_VARIANCE", 500),
        _exception("FX_VARIANCE", 600),
        _exception("REFUND_MISALLOCATION", 700),  # excluded, must not inflate the sum
    ]
    result = compute_fee_leakage(_output(exceptions))
    assert sum(c["count"] for c in result["by_category"].values()) == result["affected_records"]
    assert result["affected_records"] == 6
