"""Two reference "engines" that exist only to prove the eval harness itself is
honest, before any real matching logic is written.

null_baseline matches nothing and raises one exception per record: it must score
auto_match_rate=0%, recall=0%, false_match_rate=0%, exceptions=100% of records.

oracle_baseline reads ground_truth.json directly and asserts exactly the true link
graph, raising an exception for exactly the unresolvable defects: it must score
~100% precision/recall and false_match_rate=0.00%. It is the one place in this
codebase allowed to read ground truth, because its entire purpose is to test the
scorer, not to reconcile anything.
"""

from __future__ import annotations

import time

from .contract import RECOMMENDED_ACTIONS, EngineMeta, EngineOutput, Match, ReconException, severity_for_amount
from .io import Dataset


def null_baseline(dataset: Dataset) -> EngineOutput:
    start = time.perf_counter()
    exceptions = [
        ReconException(
            category="UNRECONCILED",
            severity=severity_for_amount(p.net_paise),
            amount_at_risk_paise=p.net_paise,
            affected={"payment_id": p.payment_id},
            recommended_action=RECOMMENDED_ACTIONS["UNRECONCILED"],
        )
        for p in dataset.payments
        if p.status == "captured"
    ]
    elapsed = time.perf_counter() - start
    return EngineOutput(matches=[], exceptions=exceptions, meta=EngineMeta(wall_clock_seconds=elapsed))


def oracle_baseline(dataset: Dataset, ground_truth: dict) -> EngineOutput:
    start = time.perf_counter()
    matches: list[Match] = []
    for link in ground_truth["links"]["order_to_payment"]:
        matches.append(Match(layer="ORACLE", link_type="order_payment", left_id=link["order_id"], right_id=link["payment_id"], confidence=1.0, evidence=("ground_truth",)))
    for link in ground_truth["links"]["payment_to_settlement"]:
        matches.append(Match(layer="ORACLE", link_type="payment_settlement", left_id=link["payment_id"], right_id=link["settlement_id"], confidence=1.0, evidence=("ground_truth",)))
    for link in ground_truth["links"]["settlement_to_bank_txn"]:
        matches.append(Match(layer="ORACLE", link_type="settlement_bank_txn", left_id=link["settlement_id"], right_id=link["bank_txn_id"], confidence=1.0, evidence=("ground_truth",)))
    for link in ground_truth["links"]["chargeback_to_payment"]:
        matches.append(Match(layer="ORACLE", link_type="chargeback_payment", left_id=link["payment_id"], right_id=link["bank_txn_id"], confidence=1.0, evidence=("ground_truth",)))

    exceptions = [
        ReconException(
            category=d["expected_exception_category"],
            severity=severity_for_amount(d["amount_at_risk_paise"]),
            amount_at_risk_paise=d["amount_at_risk_paise"],
            affected=d["affected"],
            recommended_action=RECOMMENDED_ACTIONS.get(d["expected_exception_category"], "Manual review required."),
        )
        for d in ground_truth["defects"]
        if not d["resolvable_by_engine"]
    ]
    elapsed = time.perf_counter() - start
    return EngineOutput(matches=matches, exceptions=exceptions, meta=EngineMeta(wall_clock_seconds=elapsed))
