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

from . import exceptions as exceptions_module
from . import l0_deterministic, l1_tolerance, l2_subset, l3_agent
from .contract import EngineMeta, EngineOutput
from .io import Dataset
from .llm.base import LLMClient


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
    )
    return EngineOutput(matches=all_matches, exceptions=ledger, meta=meta)
