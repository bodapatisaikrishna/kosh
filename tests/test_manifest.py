"""Task 6.3 of the hardening sprint: a run manifest must actually name the
commit, the tree state, and the exact inputs a benchmark came from - so a
"frozen" result generated from a dirty working tree, or against a silently
swapped fixture file, is detectable rather than passed off as reproducible.
"""

from __future__ import annotations

from pathlib import Path

from engine.io import load_dataset
from eval.manifest import build_run_manifest

FIXTURES = Path("data/fixtures/run_2000")


def test_manifest_has_a_commit_sha_and_dirty_flag():
    dataset = load_dataset(FIXTURES)
    manifest = build_run_manifest(FIXTURES, "full", dataset)
    assert manifest["git_commit_sha"] is None or len(manifest["git_commit_sha"]) == 40
    assert manifest["git_dirty"] in (True, False, None)


def test_manifest_record_counts_match_the_loaded_dataset():
    dataset = load_dataset(FIXTURES)
    manifest = build_run_manifest(FIXTURES, "full", dataset)
    assert manifest["record_counts"] == {
        "orders": len(dataset.orders),
        "payments": len(dataset.payments),
        "settlements": len(dataset.settlements),
        "bank": len(dataset.bank),
    }


def test_manifest_hashes_every_input_file_present():
    dataset = load_dataset(FIXTURES)
    manifest = build_run_manifest(FIXTURES, "full", dataset)
    hashes = manifest["input_file_sha256"]
    assert set(hashes) == {"orders.csv", "pg_payments.csv", "pg_settlements.csv", "bank_statement.csv"}
    assert all(len(h) == 64 for h in hashes.values())  # sha256 hex digest length


def test_manifest_hash_changes_if_the_file_content_changes(tmp_path):
    import shutil

    fixtures_copy = tmp_path / "run_2000"
    shutil.copytree(FIXTURES, fixtures_copy)
    dataset = load_dataset(fixtures_copy)
    before = build_run_manifest(fixtures_copy, "full", dataset)["input_file_sha256"]["orders.csv"]

    orders_path = fixtures_copy / "orders.csv"
    orders_path.write_text(orders_path.read_text() + "\n", encoding="utf-8")
    after = build_run_manifest(fixtures_copy, "full", dataset)["input_file_sha256"]["orders.csv"]
    assert before != after


def test_manifest_reports_missing_input_file_as_none(tmp_path):
    import shutil

    fixtures_copy = tmp_path / "run_2000"
    shutil.copytree(FIXTURES, fixtures_copy)
    dataset = load_dataset(fixtures_copy)
    (fixtures_copy / "bank_statement.csv").unlink()
    manifest = build_run_manifest(fixtures_copy, "full", dataset)
    assert manifest["input_file_sha256"]["bank_statement.csv"] is None


def test_manifest_carries_seed_and_model_through_unchanged():
    dataset = load_dataset(FIXTURES)
    manifest = build_run_manifest(FIXTURES, "full", dataset, seed=42, model_name="nvidia/nemotron-3-ultra-550b-a55b")
    assert manifest["seed"] == 42
    assert manifest["model"] == "nvidia/nemotron-3-ultra-550b-a55b"


def test_run_eval_embeds_a_manifest_with_the_right_engine_name():
    from eval.report import run_eval

    report = run_eval(FIXTURES, "l0l1")
    assert report["manifest"]["engine"] == "l0l1"
    assert report["manifest"]["record_counts"]["orders"] > 0
