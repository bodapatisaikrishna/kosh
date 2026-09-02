"""Task 4: all-LLM ablation - measured evidence that deterministic-first is the
right architecture, not a rationalization for low LLM usage.

Bypasses L0, L1, L2, AND L4 entirely (see engine/pipeline.py::run_llm_only's
own docstring for why L4 is skipped too, not just L0-L2): every payment,
settlement, and bank row in data/fixtures/sample_200 (202 + 71 + 75 = 348
records - a 200-record *order* sample, not 200 total investigations) is
routed straight to L3, with zero deterministic pre-classification.

A real, costed run - not part of pytest, never invoked by eval/report.py's
CLI (which keeps "llm-only" at client=None for the same accidental-spend
safety "full" already has). Requires NIM_API_KEY in the environment - never
printed or logged.

    python -m scripts.run_ablation_llm_only
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from engine.io import load_dataset
from engine.llm.nim_client import NimClient
from engine.pipeline import run_llm_only
from eval.io import load_ground_truth
from eval.metrics import compute_metrics

MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
FIXTURES = Path("data/fixtures/sample_200")
OUT_PATH = Path("benchmarks/ablation_llm_only.json")


def main() -> None:
    dataset = load_dataset(FIXTURES)
    ground_truth = load_ground_truth(FIXTURES)
    client = NimClient(model=MODEL)

    scope_counts = {
        "payments": len(dataset.payments),
        "settlements": len(dataset.settlements),
        "bank": len(dataset.bank),
    }
    print(f"Routing {sum(scope_counts.values())} records to L3 directly: {scope_counts}")

    started = time.time()
    engine_output = run_llm_only(
        dataset, client=client, model_name=MODEL, backend_name="nim",
        traces_dir=Path("traces_ablation"), cache_dir=Path("traces_ablation/.cache"),
    )
    elapsed = time.time() - started

    metrics = compute_metrics(dataset, engine_output, ground_truth)
    accuracy = metrics["accuracy"]

    report = {
        "note": (
            "All-LLM ablation (Task 4): L0, L1, L2, and L4 all bypassed - every "
            "payment/settlement/bank row routed straight to L3 with zero "
            "deterministic pre-classification. Orders are not independently "
            "investigated (only reachable as get_record context, same as the "
            "real pipeline). Compare against the L0/L0+L1/L0+L1+L2/full row in "
            "README.md's ablation table - this row is on data/fixtures/sample_200 "
            "(348 non-order records), not the 2000-record run_2000 used everywhere "
            "else in that table, because routing every record through a live LLM "
            "at 2000-record scale would be materially more expensive and slower "
            "for the same architectural point."
        ),
        "scope": "payments + settlements + bank (orders excluded, L4 bypassed)",
        "scope_counts": scope_counts,
        "backend": "nim",
        "model": MODEL,
        "fixtures": str(FIXTURES),
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
        "exceptions_total": metrics["exceptions"]["count"],
    }
    OUT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
