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
from engine.contract import EngineOutput, Match
from engine.io import load_dataset
from eval.io import load_ground_truth
from eval.metrics import compute_metrics

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "run_2000"
BASELINES_DIR = Path(__file__).resolve().parent / "baselines"

# The count both README.md and RESULTS.md quote when describing the mutation
# test ("inject 10 wrong links and it reports 0.24%").
WRONG_LINKS_TO_INJECT = 10


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


def test_injecting_wrong_links_is_caught_by_the_harness():
    """Mutation test: 0.00% false-match is only meaningful if the harness
    demonstrably reports a NON-zero rate when the engine is wrong. Both
    README.md and RESULTS.md make exactly this claim and cite this file by
    name, so the claim lives here as a runnable test rather than as prose.

    Takes the oracle's known-perfect output and appends 10 deliberately wrong
    payment->settlement links, each verified absent from the true link graph
    (so the mutation is genuinely wrong, not accidentally right).
    """
    dataset, ground_truth = _load()
    output = oracle_baseline(dataset, ground_truth)
    baseline = compute_metrics(dataset, output, ground_truth)
    assert baseline["accuracy"]["false_match_rate"] == 0.0, "oracle must start clean for this to prove anything"
    baseline_asserted = baseline["accuracy"]["link_scoring"]["total_asserted_links"]

    true_pairs = {(l["payment_id"], l["settlement_id"]) for l in ground_truth["links"]["payment_to_settlement"]}
    payment_ids = [p.payment_id for p in dataset.payments]
    settlement_ids = [s.settlement_id for s in dataset.settlements]
    wrong_links = []
    i = 0
    while len(wrong_links) < WRONG_LINKS_TO_INJECT:
        pair = (payment_ids[i], settlement_ids[(i + 7) % len(settlement_ids)])
        if pair not in true_pairs:
            wrong_links.append(Match(layer="MUTANT", link_type="payment_settlement", left_id=pair[0], right_id=pair[1], confidence=1.0))
        i += 1

    mutated = EngineOutput(matches=output.matches + wrong_links, exceptions=output.exceptions, meta=output.meta)
    metrics = compute_metrics(dataset, mutated, ground_truth)

    expected_rate = WRONG_LINKS_TO_INJECT / (baseline_asserted + WRONG_LINKS_TO_INJECT)
    assert metrics["accuracy"]["false_match_rate"] == expected_rate
    assert metrics["accuracy"]["precision"] < 1.0
    # The specific number both docs quote. If the reference fixture is ever
    # regenerated at a different size this assertion fails, which correctly
    # forces the documented figure to be updated with it rather than silently
    # going stale.
    assert f"{metrics['accuracy']['false_match_rate'] * 100:.2f}" == "0.24"


def test_dropping_matches_is_caught_by_the_harness():
    """The other half of the same proof: dropping true matches must show up as
    a recall collapse, and must NOT look like a false match. That separation is
    the whole reason false_match_rate and auto_match_rate are always reported
    next to each other - an engine that asserts almost nothing scores a clean
    0.00% false-match while being useless, and recall is what exposes it."""
    dataset, ground_truth = _load()
    output = oracle_baseline(dataset, ground_truth)
    assert compute_metrics(dataset, output, ground_truth)["accuracy"]["recall"] == 1.0

    halved = EngineOutput(matches=output.matches[: len(output.matches) // 2], exceptions=output.exceptions, meta=output.meta)
    metrics = compute_metrics(dataset, halved, ground_truth)

    assert metrics["accuracy"]["recall"] == 0.5
    assert metrics["accuracy"]["false_match_rate"] == 0.0, "dropping true matches is not a false match"


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
