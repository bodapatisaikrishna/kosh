"""scripts/multiseed.py: proof that 0.00% false-match isn't an artifact of
seed=42, the one seed every other committed benchmark happens to use.

Calls the script's own run_one_seed() directly (regenerating each fixture
live) rather than asserting against a committed summary.json snapshot - a
snapshot could silently go stale if someone changed the engine and forgot to
regenerate it; this always exercises real, current logic.
"""

from __future__ import annotations

from scripts.multiseed import SEEDS, run_one_seed


def test_false_match_rate_is_zero_for_every_seed():
    for seed in SEEDS:
        result = run_one_seed(seed)
        assert result["false_match_rate"] == 0.0, f"seed {seed} shows a non-zero false-match rate: {result}"


def test_every_seed_produces_all_fourteen_defect_types():
    # A seed that happened to produce fewer defect types would be measuring a
    # narrower dataset than the others - not a like-for-like comparison.
    for seed in SEEDS:
        result = run_one_seed(seed)
        assert result["distinct_defect_types"] == 14, f"seed {seed} only produced {result['distinct_defect_types']} defect types"


def test_precision_is_perfect_for_every_seed():
    for seed in SEEDS:
        result = run_one_seed(seed)
        assert result["precision"] == 1.0, f"seed {seed} shows precision {result['precision']}, not 1.0"
