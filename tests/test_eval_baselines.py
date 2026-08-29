"""Phase 2 checkpoint: the null and oracle baselines prove the eval harness itself
is honest before any real matching logic exists.

    null baseline   -> auto_match_rate 0%, recall 0%, false_match_rate 0%,
                        100% of records exceptioned
    oracle baseline -> ~100% precision/recall, false_match_rate 0.00%

Both baselines' deterministic metrics are frozen in tests/baselines/*.json as a
regression guard: if eval/metrics.py's scoring logic drifts, or a baseline engine's
behavior drifts, this test catches it. Timing fields are excluded from the freeze
since wall-clock time is never expected to be identical between runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.baselines import null_baseline, oracle_baseline
from engine.io import load_dataset
from eval.io import load_ground_truth
from eval.metrics import compute_metrics

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "run_2000"
BASELINES_DIR = Path(__file__).resolve().parent / "baselines"


def _load():
    dataset = load_dataset(FIXTURES)
    ground_truth = load_ground_truth(FIXTURES)
    return dataset, ground_truth


def test_null_baseline_checkpoint():
    dataset, ground_truth = _load()
    output = null_baseline(dataset)
    metrics = compute_metrics(dataset, output, ground_truth)

    assert metrics["accuracy"]["auto_match_rate"] == 0.0
    assert metrics["accuracy"]["recall"] == 0.0
    assert metrics["accuracy"]["false_match_rate"] == 0.0
    assert metrics["accuracy"]["precision"] == 0.0
    assert metrics["records_pct_exceptioned"] == 1.0
    assert metrics["exceptions"]["count"] == metrics["throughput"]["records_processed"]


def test_oracle_baseline_checkpoint():
    dataset, ground_truth = _load()
    output = oracle_baseline(dataset, ground_truth)
    metrics = compute_metrics(dataset, output, ground_truth)

    assert metrics["accuracy"]["precision"] >= 0.999
    assert metrics["accuracy"]["recall"] >= 0.999
    assert metrics["accuracy"]["false_match_rate"] == 0.0
    # every unresolvable defect must show up correctly, every resolvable one must not
    for dtype, counts in metrics["accuracy"]["defect_confusion"].items():
        assert "missed" not in counts, f"{dtype} has a missed defect under the oracle"
        assert "misclassified" not in counts, f"{dtype} has a misclassified defect under the oracle"
        assert "false_exception_raised" not in counts, f"{dtype} incorrectly exceptioned under the oracle"


def _strip_timing(report_metrics: dict) -> dict:
    """Deep-copy the metrics dict without the one field that's inherently
    non-deterministic (wall_clock_seconds) and its derivative (records_per_second)."""
    stripped = json.loads(json.dumps(report_metrics))
    stripped["throughput"].pop("wall_clock_seconds", None)
    stripped["throughput"].pop("records_per_second", None)
    return stripped


def test_null_baseline_matches_frozen_regression_fixture():
    dataset, ground_truth = _load()
    output = null_baseline(dataset)
    metrics = _strip_timing(compute_metrics(dataset, output, ground_truth))
    frozen = json.loads((BASELINES_DIR / "null_baseline.json").read_text(encoding="utf-8"))
    assert metrics == frozen


def test_oracle_baseline_matches_frozen_regression_fixture():
    dataset, ground_truth = _load()
    output = oracle_baseline(dataset, ground_truth)
    metrics = _strip_timing(compute_metrics(dataset, output, ground_truth))
    frozen = json.loads((BASELINES_DIR / "oracle_baseline.json").read_text(encoding="utf-8"))
    assert metrics == frozen
