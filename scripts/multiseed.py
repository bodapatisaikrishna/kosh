"""Task 1 of the hardening sprint: prove no result is an artifact of seed=42.

Runs the complete deterministic pipeline (L0 -> L1 -> L2 -> L4's classifier,
client=None so no live LLM is ever invoked) at 2000 records across 6
different seeds, scores each against its own generated ground truth, and
reports whether auto_match_rate/false_match_rate/precision/recall are stable
across seeds or a fluke of the one seed every other benchmark in this repo
happens to use.

Each seed's fixture is generated to a throwaway temp directory and deleted
immediately after scoring - only the resulting metrics (benchmarks/multiseed/
summary.json + one seed_<n>.json each) are kept, not 6x the raw CSVs.
"""

from __future__ import annotations

import json
import shutil
import statistics
import tempfile
import time
from datetime import date
from pathlib import Path

from data.generator.generate import run as generate_run
from engine import exceptions as exceptions_module
from engine import l2_subset, pipeline
from engine.io import Dataset, load_dataset
from eval.io import load_ground_truth
from eval.metrics import compute_metrics

SEEDS = [1, 7, 42, 100, 2026, 31337]
RECORDS = 2000
MONTHS = 3
END_DATE = date(2026, 8, 31)

OUT_DIR = Path("benchmarks/multiseed")


def _residual_size(dataset: Dataset) -> int:
    """How many payments would reach L3 (the residual size), recomputed via
    the identical L0/L1/L2/classify_deterministic chain pipeline.run_full
    uses internally - deliberately not exposed as a new pipeline.py return
    value, since this script is the only thing that needs it."""
    matches, credit_residual, debit_residual = pipeline._l0_l1_matches(dataset)
    already_matched_settlement_ids = {m.left_id for m in matches if m.link_type == "settlement_bank_txn"}
    l2_matches = l2_subset.match_settlement_bank_txn(dataset, credit_residual, already_matched_settlement_ids)
    l2_matched_txn_ids = {m.right_id for m in l2_matches}
    final_credit_residual = [t for t in credit_residual if t.bank_txn_id not in l2_matched_txn_ids]
    _det_exceptions, unexplained_payment_ids = exceptions_module.classify_deterministic(
        dataset, final_credit_residual, debit_residual
    )
    return len(unexplained_payment_ids)


def run_one_seed(seed: int) -> dict:
    """Generates a fresh RECORDS-record fixture at this seed, scores it with
    the complete deterministic pipeline (no live LLM - client=None, per this
    task's explicit instruction: a non-empty L3 residual is recorded by size
    and left as exceptions, never sent to a real model), and returns its
    metrics. The generated fixture is deleted before returning."""
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"kosh_multiseed_{seed}_"))
    try:
        generate_run(records=RECORDS, seed=seed, months=MONTHS, end_date=END_DATE, out_dir=tmp_dir)
        dataset = load_dataset(tmp_dir)
        ground_truth = load_ground_truth(tmp_dir)

        started = time.perf_counter()
        engine_output = pipeline.run_full(dataset, client=None)
        wall_clock_seconds = time.perf_counter() - started

        metrics = compute_metrics(dataset, engine_output, ground_truth)
        accuracy = metrics["accuracy"]
        distinct_defect_types = len({d["type"] for d in ground_truth["defects"]})

        return {
            "seed": seed,
            "auto_match_rate": accuracy["auto_match_rate"],
            "false_match_rate": accuracy["false_match_rate"],
            "precision": accuracy["precision"],
            "recall": accuracy["recall"],
            "exceptions_count": len(engine_output.exceptions),
            "distinct_defect_types": distinct_defect_types,
            "l3_residual_size": _residual_size(dataset),
            "wall_clock_seconds": wall_clock_seconds,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _markdown_table(results: list[dict]) -> str:
    header = "| Seed | Auto-match | False-match | Precision | Recall | Exceptions | Defect types | L3 residual | Wall clock |"
    sep = "|---|---|---|---|---|---|---|---|---|"
    rows = [header, sep]
    for r in results:
        rows.append(
            f"| {r['seed']} | {r['auto_match_rate'] * 100:.2f}% | {r['false_match_rate'] * 100:.2f}% | "
            f"{r['precision'] * 100:.2f}% | {r['recall'] * 100:.2f}% | {r['exceptions_count']} | "
            f"{r['distinct_defect_types']} | {r['l3_residual_size']} | {r['wall_clock_seconds'] * 1000:.2f}ms |"
        )
    return "\n".join(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for seed in SEEDS:
        print(f"seed {seed}: generating + scoring...")
        result = run_one_seed(seed)
        results.append(result)
        (OUT_DIR / f"seed_{seed}.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if result["false_match_rate"] != 0.0:
            print(
                f"!!! seed {seed} shows a NON-ZERO false_match_rate "
                f"({result['false_match_rate']}) - this is a real bug, not a task to keep going on."
            )

    auto_match_rates = [r["auto_match_rate"] for r in results]
    mean_auto_match = statistics.mean(auto_match_rates)
    stdev_auto_match = statistics.stdev(auto_match_rates) if len(auto_match_rates) > 1 else 0.0

    summary = {
        "seeds": SEEDS,
        "records_per_seed": RECORDS,
        "results": results,
        "mean_auto_match_rate": mean_auto_match,
        "stdev_auto_match_rate": stdev_auto_match,
        "any_false_match": any(r["false_match_rate"] != 0.0 for r in results),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print()
    print(_markdown_table(results))
    print()
    print(f"mean auto_match_rate: {mean_auto_match * 100:.4f}%")
    print(f"stdev auto_match_rate: {stdev_auto_match * 100:.4f}%")
    if summary["any_false_match"]:
        print()
        print("!!! AT LEAST ONE SEED SHOWED A NON-ZERO FALSE-MATCH RATE - STOP AND INVESTIGATE BEFORE ANYTHING ELSE.")


if __name__ == "__main__":
    main()
