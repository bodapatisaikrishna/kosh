"""Runs the layer cake and packages the result as an EngineOutput.

run_l0_l1 and run_l0_l1_l2 are kept as their own functions, not folded into
run_full with an "up to which layer" flag, so a benchmark can honestly compare
what each layer actually contributed (Phase 3 / Phase 4's own checkpoints)
rather than only ever reporting the combined result. Both report zero
exceptions - that's a known, deliberate scope limit of those two entry points
(L4 doesn't exist from their point of view), not a claim that everything
reconciled; run_full is the complete pipeline.
"""

from __future__ import annotations

import time
from collections import defaultdict

from . import exceptions as exceptions_module
from . import l0_deterministic, l1_tolerance, l2_subset, l3_agent
from .contract import EngineMeta, EngineOutput, Match
from .io import BankRow, Dataset
from .llm.base import LLMClient

# A settlement can legitimately be linked to more than one bank credit - see
# _reconcile_settlement_credit_sums - but a real split's parts sum to exactly
# its net_paise, with no float involved. This only absorbs genuine paisa-level
# rounding elsewhere in the chain, not a meaningfully wrong sum.
SETTLEMENT_SUM_TOLERANCE_PAISE = 100


def _l0_l1_matches(dataset: Dataset) -> tuple[list, list, list]:
    """Returns (matches, still_unresolved_credit_rows, still_unresolved_debit_rows)
    - shared by every entry point so L2's residual (and, later, L4's) is exactly
    what L0+L1 actually left behind, not recomputed."""
    matches = []
    matches += l0_deterministic.match_order_payment(dataset)
    matches += l0_deterministic.match_payment_settlement(dataset)
    sb_matches, l0_residual = l0_deterministic.match_settlement_bank_txn(dataset)
    matches += sb_matches

    cb_matches, debit_residual = l0_deterministic.match_chargeback_payment(dataset)
    matches += cb_matches

    l1_matches = l1_tolerance.match_settlement_bank_txn(dataset, l0_residual)
    matches += l1_matches
    l1_matched_txn_ids = {m.right_id for m in l1_matches}
    still_residual = [t for t in l0_residual if t.bank_txn_id not in l1_matched_txn_ids]

    return matches, still_residual, debit_residual


def _reconcile_settlement_credit_sums(dataset: Dataset, matches: list[Match]) -> tuple[list[Match], list[BankRow]]:
    """Post-pass over every settlement_bank_txn match together, after L0+L1+L2
    have all run - none of those three layers can see the others' matches while
    running, so this is the first point anything can check the full picture for
    one settlement at once.

    Two bank rows can legitimately both belong to one settlement:
    settlement_split genuinely divides one payout across two real deposits,
    each carrying a partial amount that sums exactly to the settlement's own
    net_paise. What must never happen is a settlement's linked credits summing
    to MORE than its own net (found by the adversarial suite, Task 3 attack f:
    the identical UTR text landing on two semantically unrelated bank rows,
    which every layer happily matched independently since none of them checks
    the total against the settlement's own books).

    When the sum is over, every link for that settlement is refused rather
    than guessing which one is the real one - same "never guess" principle as
    every ambiguity guard inside a single layer, just applied across all three
    matching layers' combined output instead of within one of them. The first
    attempt at this fix instead refused any second claim outright, which
    wrongly broke genuine settlement_split resolution (11/11 on run_2000) -
    caught immediately by the cash-reconciliation identity test regressing,
    and is exactly why this is a sum check, not a one-claim-only rule.
    """
    settlement_by_id = {s.settlement_id: s for s in dataset.settlements}
    bank_by_id = {b.bank_txn_id: b for b in dataset.bank}
    by_settlement: dict[str, list[Match]] = defaultdict(list)
    for m in matches:
        if m.link_type == "settlement_bank_txn":
            by_settlement[m.left_id].append(m)

    rejected_txn_ids: set[str] = set()
    for settlement_id, group in by_settlement.items():
        if len(group) < 2:
            continue
        settlement = settlement_by_id.get(settlement_id)
        if settlement is None:
            continue
        total_credited = sum(bank_by_id[m.right_id].credit_paise for m in group if m.right_id in bank_by_id)
        if total_credited > settlement.net_paise + SETTLEMENT_SUM_TOLERANCE_PAISE:
            rejected_txn_ids.update(m.right_id for m in group)

    if not rejected_txn_ids:
        return matches, []

    kept = [m for m in matches if not (m.link_type == "settlement_bank_txn" and m.right_id in rejected_txn_ids)]
    rejected_bank_rows = [bank_by_id[tid] for tid in rejected_txn_ids if tid in bank_by_id]
    return kept, rejected_bank_rows


def run_l0_l1(dataset: Dataset) -> EngineOutput:
    """Phase 3 pipeline: exact-key cascade + tolerance matching only."""
    start = time.perf_counter()
    matches, _credit_residual, _debit_residual = _l0_l1_matches(dataset)
    elapsed = time.perf_counter() - start
    return EngineOutput(matches=matches, exceptions=[], meta=EngineMeta(wall_clock_seconds=elapsed))


def run_l0_l1_l2(dataset: Dataset) -> EngineOutput:
    """Phase 4 pipeline: adds the subset-sum solver over whatever L0/L1 couldn't
    place. On the reference fixture this residual is empty (see
    ARCHITECTURE.md) - L2 is real and tested, but contributes zero matches there."""
    start = time.perf_counter()
    matches, residual, _debit_residual = _l0_l1_matches(dataset)
    already_matched_settlement_ids = {m.left_id for m in matches if m.link_type == "settlement_bank_txn"}
    matches += l2_subset.match_settlement_bank_txn(dataset, residual, already_matched_settlement_ids)
    elapsed = time.perf_counter() - start
    return EngineOutput(matches=matches, exceptions=[], meta=EngineMeta(wall_clock_seconds=elapsed))


def run_llm_only(dataset: Dataset, client: LLMClient | None, model_name: str = "none", backend_name: str = "none", **l3_kwargs) -> EngineOutput:
    """All-LLM ablation (hardening sprint, Task 4): bypasses L0, L1, L2, AND L4
    entirely - every payment, settlement, and bank row is routed straight to
    L3, with zero deterministic pre-classification of any kind. This is the
    only reading where "all-LLM" really means all-LLM: running L4's
    deterministic rules against an unfiltered bank statement would produce
    meaningless numbers (those rules assume most of the statement is already
    explained), so L4 is skipped too, not just L0-L2.

    Orders are deliberately NOT investigated as their own residual items -
    they're only ever reachable as context via a payment's own get_record
    calls, same as in the real pipeline. Settlements and bank rows ARE
    included (not just payments) for defect-class parity with run_full:
    l3_agent.run_l3/ToolContext are already source-agnostic and needed no
    code change to accept a mixed (id, source) list - only this residual
    construction is new.

    This exists purely to measure the deterministic-first architecture
    against its alternative, not as a real reconciliation mode - eval/report.py
    deliberately keeps this at client=None in its own ENGINES dict, same
    invariant "full" already has, so a routine CLI run can never accidentally
    spend real money. The actual costed run lives in
    scripts/run_ablation_llm_only.py."""
    start = time.perf_counter()
    residual_items = (
        [(p.payment_id, "payments") for p in dataset.payments]
        + [(s.settlement_id, "settlements") for s in dataset.settlements]
        + [(b.bank_txn_id, "bank") for b in dataset.bank]
    )
    if client is None:
        elapsed = time.perf_counter() - start
        return EngineOutput(matches=[], exceptions=[], meta=EngineMeta(wall_clock_seconds=elapsed))

    l3_output = l3_agent.run_l3(dataset, residual_items, client, model_name=model_name, backend_name=backend_name, **l3_kwargs)
    ledger = exceptions_module.build_ledger(l3_output.exceptions)
    elapsed = time.perf_counter() - start
    meta = EngineMeta(
        wall_clock_seconds=elapsed,
        llm_calls=l3_output.meta.llm_calls,
        input_tokens=l3_output.meta.input_tokens,
        output_tokens=l3_output.meta.output_tokens,
        cost_usd_micros=l3_output.meta.cost_usd_micros,
    )
    return EngineOutput(matches=l3_output.matches, exceptions=ledger, meta=meta)


def run_full(dataset: Dataset, client: LLMClient | None = None, model_name: str = "none", backend_name: str = "none", **l3_kwargs) -> EngineOutput:
    """The complete pipeline: L0 (incl. chargeback matching) -> L1 -> L2 ->
    L4's deterministic classification -> L3 (only for whatever's still
    genuinely unexplained, and only if a client is given) -> the sorted
    exception ledger.

    Without a client, a genuinely unexplained payment still never vanishes -
    see engine/exceptions.py::unexplained_to_fallback_exceptions. On
    `run_2000` this residual is empty regardless (see ARCHITECTURE.md), so
    `client=None` already produces the complete, correct result there.
    """
    start = time.perf_counter()
    matches, credit_residual, debit_residual = _l0_l1_matches(dataset)
    already_matched_settlement_ids = {m.left_id for m in matches if m.link_type == "settlement_bank_txn"}
    l2_matches = l2_subset.match_settlement_bank_txn(dataset, credit_residual, already_matched_settlement_ids)
    matches += l2_matches
    l2_matched_txn_ids = {m.right_id for m in l2_matches}
    final_credit_residual = [t for t in credit_residual if t.bank_txn_id not in l2_matched_txn_ids]

    matches, over_claimed_bank_rows = _reconcile_settlement_credit_sums(dataset, matches)
    final_credit_residual = final_credit_residual + over_claimed_bank_rows

    det_exceptions, unexplained_payment_ids = exceptions_module.classify_deterministic(dataset, final_credit_residual, debit_residual)

    l3_output: EngineOutput | None = None
    if unexplained_payment_ids:
        if client is not None:
            residual_items = [(pid, "payments") for pid in sorted(unexplained_payment_ids)]
            l3_output = l3_agent.run_l3(dataset, residual_items, client, model_name=model_name, backend_name=backend_name, **l3_kwargs)
        else:
            det_exceptions += exceptions_module.unexplained_to_fallback_exceptions(dataset, unexplained_payment_ids)

    all_matches = matches + (l3_output.matches if l3_output else [])
    all_exceptions = det_exceptions + (l3_output.exceptions if l3_output else [])
    ledger = exceptions_module.build_ledger(all_exceptions)

    elapsed = time.perf_counter() - start
    meta = EngineMeta(
        wall_clock_seconds=elapsed,
        llm_calls=l3_output.meta.llm_calls if l3_output else 0,
        input_tokens=l3_output.meta.input_tokens if l3_output else 0,
        output_tokens=l3_output.meta.output_tokens if l3_output else 0,
        cost_usd_micros=l3_output.meta.cost_usd_micros if l3_output else 0,
    )
    return EngineOutput(matches=all_matches, exceptions=ledger, meta=meta)
