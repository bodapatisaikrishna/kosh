"""engine/l2_subset.py: subset-sum for a bank credit that batches multiple
candidates. Correctness first, then the ambiguity guard, then negative amounts
(refunds/chargebacks netted in), then the timeout->escalation path.
"""

from __future__ import annotations

import time

from engine.l2_subset import Candidate, SubsetSolution, solve_subset


def _c(id_, amount):
    return Candidate(id=id_, amount_paise=amount)


def test_exact_single_candidate_solves():
    result = solve_subset(1000_00, [_c("a", 1000_00), _c("b", 500_00)])
    assert result.status == "SOLVED"
    assert result.chosen_ids == ("a",)
    assert result.achieved_paise == 1000_00


def test_solves_a_genuine_batch_of_several_candidates():
    candidates = [_c("a", 120_00), _c("b", 380_00), _c("c", 999_00), _c("d", 250_00)]
    # target = a + d, uniquely (no other combination sums close to 370_00)
    result = solve_subset(370_00, candidates, tolerance_paise=0)
    assert result.status == "SOLVED"
    assert set(result.chosen_ids) == {"a", "d"}
    assert result.achieved_paise == 370_00


def test_within_tolerance_counts_as_solved():
    result = solve_subset(1000_50, [_c("a", 1000_00)], tolerance_paise=100)
    assert result.status == "SOLVED"
    assert result.chosen_ids == ("a",)


def test_outside_tolerance_is_none():
    result = solve_subset(1005_00, [_c("a", 1000_00)], tolerance_paise=100)
    assert result.status == "NONE"


def test_no_candidates_is_none():
    assert solve_subset(1000_00, []).status == "NONE"


def test_ambiguous_when_two_distinct_subsets_hit_the_target():
    # {a} and {b} both equal 500_00 exactly - genuinely ambiguous, must refuse.
    candidates = [_c("a", 500_00), _c("b", 500_00), _c("c", 999_00)]
    result = solve_subset(500_00, candidates, tolerance_paise=0)
    assert result.status == "AMBIGUOUS"
    assert {frozenset(s) for s in result.alternative_solutions} == {frozenset(["a"]), frozenset(["b"])}
    assert result.chosen_ids == ()


def test_ambiguous_across_different_sized_subsets():
    # {a,b} == 300_00 and {c} == 300_00 - still two distinct subsets, still ambiguous.
    candidates = [_c("a", 100_00), _c("b", 200_00), _c("c", 300_00)]
    result = solve_subset(300_00, candidates, tolerance_paise=0)
    assert result.status == "AMBIGUOUS"


def test_negative_amount_refund_netted_into_target():
    # settlement = payment - refund: 1000_00 - 200_00 = 800_00
    candidates = [_c("payment", 1000_00), _c("refund", -200_00)]
    result = solve_subset(800_00, candidates, tolerance_paise=0)
    assert result.status == "SOLVED"
    assert set(result.chosen_ids) == {"payment", "refund"}


def test_negative_amount_candidate_can_be_excluded():
    # target explained by the payment alone, refund not part of this batch
    candidates = [_c("payment", 1000_00), _c("unrelated_refund", -200_00)]
    result = solve_subset(1000_00, candidates, tolerance_paise=0)
    assert result.status == "SOLVED"
    assert result.chosen_ids == ("payment",)


def test_too_many_candidates_raises():
    import pytest
    candidates = [_c(f"c{i}", 100_00) for i in range(41)]
    with pytest.raises(Exception):
        solve_subset(100_00, candidates, max_terms=40)


def test_timeout_returns_timeout_status_not_a_hang():
    # An adversarial instance with many candidates all sharing the same amount and
    # a target with a huge number of equal-cardinality solutions - forces deep
    # exploration. A near-zero time budget must return TIMEOUT quickly rather than
    # exhaustively search or silently pick a wrong answer.
    candidates = [_c(f"c{i}", 100_00) for i in range(40)]
    started = time.perf_counter()
    result = solve_subset(2000_00, candidates, tolerance_paise=0, time_budget_seconds=0.001)
    elapsed = time.perf_counter() - started
    assert result.status in ("TIMEOUT", "AMBIGUOUS", "SOLVED")
    assert elapsed < 1.0


# --- match_settlement_bank_txn: the pipeline-facing wrapper ------------------

from engine.io import BankRow, Dataset, SettlementRow  # noqa: E402
from engine.l2_subset import match_settlement_bank_txn  # noqa: E402


def test_wrapper_solves_a_genuine_multi_settlement_batch():
    """One bank credit that is the sum of two settlements, no exact key or
    tolerance signal available (e.g. both UTRs missing/unrecoverable) - this is
    the "one credit is a batch" case from the brief."""
    settlements = [
        SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 310_00),
        SettlementRow("setl_b", "2026-08-02", "ICICN00000000002", 1, 0, 0, 0, 0, 690_00),
        SettlementRow("setl_c", "2026-08-01", "UTIBN00000000003", 1, 0, 0, 0, 0, 50_00),
    ]
    txn = BankRow("btxn_1", "2026-08-02", "BATCH SETTLEMENT CREDIT", 1000_00, 0, 0)
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=[txn])

    matches = match_settlement_bank_txn(dataset, [txn])
    assert {m.left_id for m in matches} == {"setl_a", "setl_b"}
    assert all(m.right_id == "btxn_1" and m.layer == "L2" for m in matches)


def test_wrapper_excludes_settlements_already_matched_elsewhere():
    settlements = [
        SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 300_00),
        SettlementRow("setl_b", "2026-08-02", "ICICN00000000002", 1, 0, 0, 0, 0, 700_00),
    ]
    txn = BankRow("btxn_1", "2026-08-02", "BATCH SETTLEMENT CREDIT", 1000_00, 0, 0)
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=[txn])

    # setl_a already claimed by L0/L1 for a different credit - excluding it means
    # no valid batch remains, so this credit is correctly left unmatched rather
    # than double-booking setl_a.
    matches = match_settlement_bank_txn(dataset, [txn], already_matched_settlement_ids={"setl_a"})
    assert matches == []


def test_wrapper_refuses_a_single_candidate_match():
    """A one-settlement 'batch' is exactly an L0/L1 case, not L2's job - if it
    reached here unmatched, something else is wrong; asserting it anyway would
    blur which layer actually earned the match."""
    settlements = [SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 1000_00)]
    txn = BankRow("btxn_1", "2026-08-01", "UNRECOGNIZABLE CREDIT", 1000_00, 0, 0)
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=[txn])

    matches = match_settlement_bank_txn(dataset, [txn])
    assert matches == []


def test_wrapper_leaves_ambiguous_batches_unmatched():
    settlements = [
        SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 500_00),
        SettlementRow("setl_b", "2026-08-01", "ICICN00000000002", 1, 0, 0, 0, 0, 500_00),
        SettlementRow("setl_c", "2026-08-01", "UTIBN00000000003", 1, 0, 0, 0, 0, 1000_00),
    ]
    # {a,b} and {c} both sum to 1000_00 - genuinely ambiguous.
    txn = BankRow("btxn_1", "2026-08-01", "BATCH CREDIT", 1000_00, 0, 0)
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=[txn])

    matches = match_settlement_bank_txn(dataset, [txn])
    assert matches == []


def test_wrapper_respects_the_date_window():
    settlements = [
        SettlementRow("setl_a", "2026-08-01", "HDFCN00000000001", 1, 0, 0, 0, 0, 300_00),
        SettlementRow("setl_far", "2026-08-20", "ICICN00000000002", 1, 0, 0, 0, 0, 700_00),
    ]
    txn = BankRow("btxn_1", "2026-08-02", "BATCH CREDIT", 1000_00, 0, 0)
    dataset = Dataset(orders=[], payments=[], settlements=settlements, bank=[txn])

    # setl_far is 18 days away - outside the date window, so the batch can't be
    # completed and this credit is correctly left unmatched.
    matches = match_settlement_bank_txn(dataset, [txn])
    assert matches == []
