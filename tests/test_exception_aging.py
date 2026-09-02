"""eval/metrics.py::compute_exception_summary's "aging" sub-dict - median/max/
buckets/breaching_48h_sla/amount_at_risk_over_30d_paise, aggregated from each
exception's own aging_days.

The 48h-SLA framing matters here as much as the number: run_2000 is a fixed,
historical 3-month fixture scored against its own end date, not a live queue,
so a large "breaching" count is the expected result of scoring a static
snapshot - not a finding about operational neglect. See ARCHITECTURE.md.
"""

from __future__ import annotations

from engine.contract import EngineMeta, EngineOutput, ReconException
from eval.metrics import compute_exception_summary


def _exception(aging_days: int, amount_paise: int = 100) -> ReconException:
    return ReconException(
        category="UNIDENTIFIED_CREDIT", severity="STANDARD", amount_at_risk_paise=amount_paise,
        affected={"bank_txn_id": f"btxn_age{aging_days}"}, recommended_action="x", aging_days=aging_days,
    )


def _output(exceptions: list[ReconException]) -> EngineOutput:
    return EngineOutput(matches=[], exceptions=exceptions, meta=EngineMeta(wall_clock_seconds=0.0))


def test_empty_exceptions_yields_all_zeros_no_crash():
    aging = compute_exception_summary(_output([]))["aging"]
    assert aging["median_days"] == 0
    assert aging["max_days"] == 0
    assert aging["breaching_48h_sla"] == 0
    assert aging["amount_at_risk_over_30d_paise"] == 0
    assert aging["buckets"] == {"0-2d": 0, "3-7d": 0, "8-30d": 0, "30d+": 0}


def test_buckets_sum_to_total_exception_count():
    exceptions = [_exception(a) for a in [0, 1, 2, 3, 5, 7, 8, 15, 30, 31, 60, 89]]
    summary = compute_exception_summary(_output(exceptions))
    aging = summary["aging"]
    assert sum(aging["buckets"].values()) == summary["count"] == len(exceptions)
    assert aging["buckets"] == {"0-2d": 3, "3-7d": 3, "8-30d": 3, "30d+": 3}


def test_breaching_48h_sla_equals_count_with_aging_over_two_days():
    exceptions = [_exception(a) for a in [0, 1, 2, 3, 4, 100]]
    aging = compute_exception_summary(_output(exceptions))["aging"]
    # aging_days 0/1/2 are within the 48h SLA; 3/4/100 breach it
    assert aging["breaching_48h_sla"] == 3
    assert aging["breaching_48h_sla"] == sum(1 for e in exceptions if e.aging_days > 2)


def test_known_fixture_hand_computed_median_and_max():
    ages = [1, 5, 9, 40, 89]  # odd count - median is the middle element once sorted
    exceptions = [_exception(a) for a in ages]
    aging = compute_exception_summary(_output(exceptions))["aging"]
    assert aging["median_days"] == 9
    assert aging["max_days"] == 89


def test_amount_at_risk_over_30d_paise_only_counts_the_30d_plus_bucket():
    exceptions = [
        _exception(10, amount_paise=1_000),  # 8-30d bucket, not counted
        _exception(31, amount_paise=2_000),  # 30d+, counted
        _exception(89, amount_paise=3_000),  # 30d+, counted
    ]
    aging = compute_exception_summary(_output(exceptions))["aging"]
    assert aging["amount_at_risk_over_30d_paise"] == 2_000 + 3_000
