"""Runs L3 for real against the genuine run_2000 residual - the payments L0-L2
matching and L4's deterministic classifier could not explain - on a real LLM
backend, and writes benchmarks/phase5_live_residual.json plus per-record
traces to traces/ (committed samples live under benchmarks/sample_traces_live/,
copied there by hand after independent verification, not by this script).

Formalizes what was previously a throwaway /tmp script, so re-runs (a fix to
L3's tool layer, a turn-budget change, a different model) are reproducible
and reviewable rather than one-off. Requires NIM_API_KEY in the environment -
never printed or logged.

    python -m scripts.run_live_residual
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from engine.io import load_dataset
from engine.llm.nim_client import NimClient
from engine.pipeline import run_full
from eval.io import load_ground_truth
from eval.metrics import compute_metrics

MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
FIXTURES = Path("data/fixtures/run_2000")
OUT_PATH = Path("benchmarks/phase5_live_residual.json")


def main() -> None:
    dataset = load_dataset(FIXTURES)
    ground_truth = load_ground_truth(FIXTURES)
    client = NimClient(model=MODEL)

    started = time.time()
    engine_output = run_full(
        dataset, client=client, model_name=MODEL, backend_name="nim",
        traces_dir=Path("traces"), cache_dir=Path("traces/.cache"),
    )
    elapsed = time.time() - started

    metrics = compute_metrics(dataset, engine_output, ground_truth)
    accuracy = metrics["accuracy"]

    report = {
        "note": (
            "L3 run against the REAL residual from data/fixtures/run_2000 - the payments "
            "that L0-L2 matching and L4's deterministic classifier genuinely could not "
            "explain. Not a synthetic exercise set. All records independently cross-checked "
            "against ground_truth.json by hand (link correctness, category, and "
            "amount_at_risk_paise), not just read off this summary's own scores."
        ),
        "backend": "nim",
        "model": MODEL,
        "wall_clock_seconds": elapsed,
        "llm_calls": engine_output.meta.llm_calls,
        "input_tokens": engine_output.meta.input_tokens,
        "output_tokens": engine_output.meta.output_tokens,
        "cost_usd_micros": engine_output.meta.cost_usd_micros,
        "cost_per_1000_records_micros": (
            (engine_output.meta.cost_usd_micros * 1000) // accuracy["total_records"] if accuracy["total_records"] else 0
        ),
        "auto_match_rate": accuracy["auto_match_rate"],
        "false_match_rate": accuracy["false_match_rate"],
        "precision": accuracy["precision"],
        "recall": accuracy["recall"],
        "layer_contribution": accuracy["layer_contribution"],
        "exceptions_total": metrics["exceptions"]["count"],
        "defect_confusion": accuracy["defect_confusion"],
    }
    OUT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "defect_confusion"}, indent=2, default=str))
    print()
    print("defect_confusion:", json.dumps(report["defect_confusion"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
