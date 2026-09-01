"""Task 6.1 of the hardening sprint: running the same unchanged input twice
must produce byte-identical output (deterministic), and must never produce a
duplicate link or ledger entry (idempotent) - two runs of the same fixture
never accumulate anything.

"Idempotent" here means: no persistence/re-ingestion layer exists in this
codebase (rule 2 forbids building new reconciliation infrastructure), so this
is read as "two runs of the same unchanged input agree exactly on content,
with no duplicate entries appearing" - not literal re-ingestion of a prior
run's own output as new input, which has no code path to test.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.report import run_eval

FIXTURES = Path("data/fixtures/run_2000")

# Keys whose values are allowed (expected) to differ between two runs of the
# same input - wall-clock timing and the timestamp the report was generated
# at. Nothing else should ever move.
_VOLATILE_TOP_LEVEL_KEYS = ("generated_at_unix",)
_VOLATILE_THROUGHPUT_KEYS = ("wall_clock_seconds", "records_per_second")
_VOLATILE_MANIFEST_KEYS = ("generated_at_utc",)


def _strip_volatile(report: dict) -> dict:
    report = dict(report)
    for key in _VOLATILE_TOP_LEVEL_KEYS:
        report.pop(key, None)
    throughput = report.get("metrics", {}).get("throughput")
    if throughput is not None:
        for key in _VOLATILE_THROUGHPUT_KEYS:
            throughput.pop(key, None)
    manifest = report.get("manifest")
    if manifest is not None:
        for key in _VOLATILE_MANIFEST_KEYS:
            manifest.pop(key, None)
    return report


def test_two_runs_of_run_2000_are_byte_identical_excluding_timing():
    first = _strip_volatile(run_eval(FIXTURES, "full"))
    second = _strip_volatile(run_eval(FIXTURES, "full"))
    first_json = json.dumps(first, sort_keys=True, default=str)
    second_json = json.dumps(second, sort_keys=True, default=str)
    assert first_json == second_json


def test_two_runs_never_produce_duplicate_matches_or_exceptions():
    from engine.io import load_dataset
    from engine.pipeline import run_full

    dataset = load_dataset(FIXTURES)
    first = run_full(dataset, client=None)
    second = run_full(dataset, client=None)

    def match_keys(output):
        return [(m.layer, m.link_type, m.left_id, m.right_id) for m in output.matches]

    def exception_keys(output):
        return [(e.category, tuple(sorted(e.affected.items()))) for e in output.exceptions]

    # Same content, no duplicates introduced by running twice.
    assert sorted(match_keys(first)) == sorted(match_keys(second))
    assert sorted(exception_keys(first)) == sorted(exception_keys(second))
    assert len(match_keys(first)) == len(set(match_keys(first)))  # no duplicates within one run either
    assert len(exception_keys(first)) == len(set(exception_keys(first)))
