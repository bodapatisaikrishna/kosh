"""Runs the layer cake and packages the result as an EngineOutput.

L3 (Claude agent) and L4 (exception ledger) don't exist yet. Whatever a pipeline
leaves unresolved is simply absent from `matches` for now; it becomes L3's residual
queue in Phase 5, and only then does an honest exception ledger get raised for it.
Reporting zero exceptions here is a known, temporary limitation, not a claim that
everything reconciled.

Two entry points are kept separate (not one function with an "up to which layer"
flag) so a benchmark can honestly compare what each layer actually contributes,
rather than only ever reporting the combined result.
"""

from __future__ import annotations

import time

from . import l0_deterministic, l1_tolerance, l2_subset
from .contract import EngineMeta, EngineOutput
from .io import Dataset


def _l0_l1_matches(dataset: Dataset) -> tuple[list, list]:
    """Returns (matches, still_unresolved_bank_rows) - shared by both entry points
    so L2's residual is exactly what L0+L1 actually left behind, not recomputed."""
    matches = []
    matches += l0_deterministic.match_order_payment(dataset)
    matches += l0_deterministic.match_payment_settlement(dataset)
    sb_matches, l0_residual = l0_deterministic.match_settlement_bank_txn(dataset)
    matches += sb_matches

    l1_matches = l1_tolerance.match_settlement_bank_txn(dataset, l0_residual)
    matches += l1_matches
    l1_matched_txn_ids = {m.right_id for m in l1_matches}
    still_residual = [t for t in l0_residual if t.bank_txn_id not in l1_matched_txn_ids]

    return matches, still_residual


def run_l0_l1(dataset: Dataset) -> EngineOutput:
    """Phase 3 pipeline: exact-key cascade + tolerance matching only."""
    start = time.perf_counter()
    matches, _residual = _l0_l1_matches(dataset)
    elapsed = time.perf_counter() - start
    return EngineOutput(matches=matches, exceptions=[], meta=EngineMeta(wall_clock_seconds=elapsed))


def run_l0_l1_l2(dataset: Dataset) -> EngineOutput:
    """Phase 4 pipeline: adds the subset-sum solver over whatever L0/L1 couldn't
    place. On the reference fixture this residual is empty (see
    ARCHITECTURE.md) - L2 is real and tested, but contributes zero matches there."""
    start = time.perf_counter()
    matches, residual = _l0_l1_matches(dataset)
    already_matched_settlement_ids = {m.left_id for m in matches if m.link_type == "settlement_bank_txn"}
    matches += l2_subset.match_settlement_bank_txn(dataset, residual, already_matched_settlement_ids)
    elapsed = time.perf_counter() - start
    return EngineOutput(matches=matches, exceptions=[], meta=EngineMeta(wall_clock_seconds=elapsed))
