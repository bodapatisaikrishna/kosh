"""Runs the layer cake, L0 then L1, and packages the result as an EngineOutput.

L2 (subset-sum), L3 (Claude agent), and L4 (exception ledger) don't exist yet -
this is Phase 3. Whatever L0/L1 leave unresolved is simply absent from `matches`
for now; it will become L2/L3's residual queue in Phases 4-5, and only then does an
honest exception ledger get raised for it. Reporting zero exceptions here is a known,
temporary Phase 3 limitation, not a claim that everything reconciled.
"""

from __future__ import annotations

import time

from . import l0_deterministic, l1_tolerance
from .contract import EngineMeta, EngineOutput
from .io import Dataset


def run_l0_l1(dataset: Dataset) -> EngineOutput:
    start = time.perf_counter()

    matches = []
    matches += l0_deterministic.match_order_payment(dataset)
    matches += l0_deterministic.match_payment_settlement(dataset)
    sb_matches, residual = l0_deterministic.match_settlement_bank_txn(dataset)
    matches += sb_matches
    matches += l1_tolerance.match_settlement_bank_txn(dataset, residual)

    elapsed = time.perf_counter() - start
    return EngineOutput(matches=matches, exceptions=[], meta=EngineMeta(wall_clock_seconds=elapsed))
