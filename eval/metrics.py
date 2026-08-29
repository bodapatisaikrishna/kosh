"""Scores an EngineOutput against ground_truth.json.

Definitions used throughout this module (documented here once, not scattered):

- "record" = a captured payment. This is the reconciliation unit: it's the thing
  that either ends up correctly linked all the way to a bank credit, or ends up on
  the exception ledger, or (an engine bug) neither. Orders, settlements and bank
  txns exist to be linked *to* a payment's chain, not as separate countable units.
- A payment's chain is "fully and correctly matched" when every true link segment
  that should exist for it (payment->settlement, and settlement->bank_txn if that
  settlement has exactly one bank credit) is present among the engine's asserted
  matches. A payment with no true settlement link (defects #1 missing_settlement,
  #2 duplicate_payment) can never be "matched" - it should end up on the exception
  ledger instead, and auto_match_rate scores it as unmatched, correctly.
- false_match_rate is computed against the engine's OWN assertions (wrong / total
  asserted), not against total records: a false match is a false match whether the
  engine asserted one link or ten thousand. This is deliberate - it is possible to
  hide a bad false-match rate by asserting very few links, so the eval report
  always prints false_match_rate next to auto_match_rate, never alone.
- hands_off_rate is currently defined as equal to auto_match_rate. This is a known
  Phase 2 simplification, stated explicitly rather than silently: L0-L1 (Phase 3)
  are the first layers that can leave a record neither matched nor exceptioned
  (e.g. a low-confidence L1 candidate that isn't escalated), which would make the
  two diverge. Until then they are the same number by construction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict

from engine.contract import LINK_TYPES, EngineOutput
from engine.io import Dataset

RESOLVABLE_DEFECT_TYPES = {"rounding_drift", "utr_mangled", "settlement_split"}

# Which field(s) of a defect's `affected` dict actually identify it, for matching
# against an exception's `affected` dict in compute_defect_confusion. This must be
# narrower than "any shared id": a settlement can independently carry both a
# payment-level defect (e.g. fee_mismatch_wrong_tier, whose affected dict also
# carries that settlement_id for context) and a settlement-level defect (e.g.
# settlement_split) at the same time. Matching on settlement_id alone would let the
# unrelated payment-level exception get credited (or blamed) for the settlement-level
# defect, and vice versa - so each defect type is matched only on the field(s) that
# are actually its own identity, not on incidental context fields.
DEFECT_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "missing_settlement": ("payment_id",),
    "duplicate_payment": ("duplicate_payment_id",),
    "rounding_drift": ("payment_id",),
    "fee_mismatch_wrong_tier": ("payment_id",),
    "gst_variance": ("payment_id",),
    "refund_misallocation": ("payment_id",),
    "orphan_chargeback": ("bank_txn_id",),
    "period_cutoff": ("settlement_id",),
    "utr_mangled": ("bank_txn_id",),
    "fx_variance": ("payment_id",),
    "unidentified_credit": ("bank_txn_id",),
    "settlement_split": ("bank_txn_id_a", "bank_txn_id_b"),
}


def _true_link_sets(ground_truth: dict) -> dict[str, set[tuple[str, str]]]:
    links = ground_truth["links"]
    return {
        "order_payment": {(l["order_id"], l["payment_id"]) for l in links["order_to_payment"]},
        "payment_settlement": {(l["payment_id"], l["settlement_id"]) for l in links["payment_to_settlement"]},
        "settlement_bank_txn": {(l["settlement_id"], l["bank_txn_id"]) for l in links["settlement_to_bank_txn"]},
    }


def _asserted_link_sets(engine_output: EngineOutput) -> dict[str, set[tuple[str, str]]]:
    asserted: dict[str, set[tuple[str, str]]] = {link_type: set() for link_type in LINK_TYPES}
    for m in engine_output.matches:
        asserted[m.link_type].add((m.left_id, m.right_id))
    return asserted


def _link_layer_index(engine_output: EngineOutput) -> dict[tuple[str, str, str], str]:
    """(link_type, left_id, right_id) -> layer, for layer_contribution attribution."""
    return {(m.link_type, m.left_id, m.right_id): m.layer for m in engine_output.matches}


def compute_throughput(engine_output: EngineOutput, records_processed: int) -> dict:
    wall = engine_output.meta.wall_clock_seconds
    rps = (records_processed / wall) if wall > 0 else float(records_processed) if records_processed else 0.0
    cost_micros = engine_output.meta.cost_usd_micros
    cost_per_1000_micros = (cost_micros * 1000) // records_processed if records_processed else 0
    return {
        "records_processed": records_processed,
        "wall_clock_seconds": wall,
        "records_per_second": rps,
        "llm_calls": engine_output.meta.llm_calls,
        "input_tokens": engine_output.meta.input_tokens,
        "output_tokens": engine_output.meta.output_tokens,
        "cost_usd_micros": cost_micros,
        "cost_per_1000_records_micros": cost_per_1000_micros,
    }


def compute_link_scoring(engine_output: EngineOutput, ground_truth: dict) -> dict:
    true_sets = _true_link_sets(ground_truth)
    asserted_sets = _asserted_link_sets(engine_output)

    per_link_type = {}
    total_correct = total_asserted = total_true = 0
    for link_type in LINK_TYPES:
        true_set = true_sets[link_type]
        asserted_set = asserted_sets[link_type]
        correct = true_set & asserted_set
        wrong = asserted_set - true_set
        missing = true_set - asserted_set
        per_link_type[link_type] = {
            "true_count": len(true_set),
            "asserted_count": len(asserted_set),
            "correct_count": len(correct),
            "wrong_count": len(wrong),
            "missing_count": len(missing),
        }
        total_correct += len(correct)
        total_asserted += len(asserted_set)
        total_true += len(true_set)

    precision = (total_correct / total_asserted) if total_asserted else 0.0
    recall = (total_correct / total_true) if total_true else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    false_match_rate = ((total_asserted - total_correct) / total_asserted) if total_asserted else 0.0

    return {
        "per_link_type": per_link_type,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_match_rate": false_match_rate,
        "total_correct_links": total_correct,
        "total_asserted_links": total_asserted,
        "total_true_links": total_true,
    }


def compute_layer_contribution(engine_output: EngineOutput, ground_truth: dict) -> dict:
    true_sets = _true_link_sets(ground_truth)
    layer_index = _link_layer_index(engine_output)
    counts: Counter[str] = Counter()
    for link_type, pairs in true_sets.items():
        for left, right in pairs:
            layer = layer_index.get((link_type, left, right))
            if layer is not None:
                counts[layer] += 1
    total = sum(counts.values())
    return {layer: (count / total if total else 0.0) for layer, count in sorted(counts.items())}


def compute_auto_match_rate(dataset: Dataset, engine_output: EngineOutput, ground_truth: dict) -> tuple[float, int, int]:
    true_sets = _true_link_sets(ground_truth)
    asserted_sets = _asserted_link_sets(engine_output)

    true_settlement_of_payment: dict[str, str] = dict(true_sets["payment_settlement"])
    bank_txns_of_settlement: dict[str, list[str]] = {}
    for settlement_id, bank_txn_id in true_sets["settlement_bank_txn"]:
        bank_txns_of_settlement.setdefault(settlement_id, []).append(bank_txn_id)

    def fully_matched(payment_id: str) -> bool:
        settlement_id = true_settlement_of_payment.get(payment_id)
        if settlement_id is None:
            return False  # missing_settlement / duplicate_payment - no true link to match
        if (payment_id, settlement_id) not in asserted_sets["payment_settlement"]:
            return False
        linked_bank_txns = bank_txns_of_settlement.get(settlement_id, [])
        if len(linked_bank_txns) != 1:
            return True  # split settlements: payment-level matching doesn't require resolving the split here
        return (settlement_id, linked_bank_txns[0]) in asserted_sets["settlement_bank_txn"]

    captured = [p for p in dataset.payments if p.status == "captured"]
    matched = sum(1 for p in captured if fully_matched(p.payment_id))
    total = len(captured)
    return (matched / total if total else 0.0), matched, total


def compute_defect_confusion(engine_output: EngineOutput, ground_truth: dict) -> dict:
    """Per-defect-class confusion: detected / missed / misclassified for the 9
    non-resolvable defect types, and correctly_resolved / false_exception_raised for
    the 3 resolvable ones (rounding_drift, utr_mangled, settlement_split)."""
    exceptions = engine_output.exceptions
    result: dict[str, Counter] = {}
    for defect in ground_truth["defects"]:
        dtype = defect["type"]
        identity_fields = DEFECT_IDENTITY_FIELDS[dtype]
        identity_ids = {defect["affected"][f] for f in identity_fields if f in defect["affected"]}
        matching = [e for e in exceptions if identity_ids & set(e.affected.values())]
        bucket = result.setdefault(dtype, Counter())
        if dtype in RESOLVABLE_DEFECT_TYPES:
            if matching:
                bucket["false_exception_raised"] += 1
            else:
                bucket["correctly_resolved"] += 1
        else:
            if not matching:
                bucket["missed"] += 1
            elif any(e.category == defect["expected_exception_category"] for e in matching):
                bucket["detected"] += 1
            else:
                bucket["misclassified"] += 1
    return {dtype: dict(counter) for dtype, counter in sorted(result.items())}


def compute_exception_summary(engine_output: EngineOutput) -> dict:
    exceptions = engine_output.exceptions
    by_category = Counter(e.category for e in exceptions)
    by_severity = Counter(e.severity for e in exceptions)
    return {
        "count": len(exceptions),
        "total_amount_at_risk_paise": sum(e.amount_at_risk_paise for e in exceptions),
        "by_category": dict(sorted(by_category.items())),
        "by_severity": dict(sorted(by_severity.items())),
    }


def compute_metrics(dataset: Dataset, engine_output: EngineOutput, ground_truth: dict) -> dict:
    captured_count = sum(1 for p in dataset.payments if p.status == "captured")
    auto_match_rate, matched_count, total_records = compute_auto_match_rate(dataset, engine_output, ground_truth)
    link_scoring = compute_link_scoring(engine_output, ground_truth)
    exception_summary = compute_exception_summary(engine_output)

    return {
        "throughput": compute_throughput(engine_output, captured_count),
        "accuracy": {
            "auto_match_rate": auto_match_rate,
            "matched_records": matched_count,
            "total_records": total_records,
            "hands_off_rate": auto_match_rate,  # Phase 2 simplification - see module docstring
            "layer_contribution": compute_layer_contribution(engine_output, ground_truth),
            "false_match_rate": link_scoring["false_match_rate"],
            "precision": link_scoring["precision"],
            "recall": link_scoring["recall"],
            "f1": link_scoring["f1"],
            "link_scoring": link_scoring,
            "defect_confusion": compute_defect_confusion(engine_output, ground_truth),
        },
        "exceptions": exception_summary,
        "records_pct_exceptioned": (exception_summary["count"] / total_records) if total_records else 0.0,
    }
