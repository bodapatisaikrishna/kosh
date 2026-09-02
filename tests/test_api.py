"""api/: the brief's optional FastAPI stretch goal, built post-freeze.

Covers the one thing that actually matters for a project with a code freeze
and a real-money safety invariant: the live-run endpoint must be exactly as
safe as eval/report.py's own CLI (client=None, deterministic engines only),
and must produce the SAME numbers as the frozen benchmark it reproduces -
proving the API is a thin wrapper around trusted code, not a second
implementation that could quietly drift from it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api.runs import ENGINE_ALLOWLIST  # noqa: E402

client = TestClient(app)

FREEZE_2000 = Path(__file__).resolve().parent.parent / "benchmarks" / "freeze_2000.json"


def _strip_volatile(report: dict) -> dict:
    report = dict(report)
    report.pop("generated_at_unix", None)
    report.pop("manifest", None)
    # The live endpoint generates into a scratch directory named run_<records>
    # (see api/runs.py's own comment on why), not data/fixtures/run_2000 - the
    # path differs even though the content is byte-identical, so it's not
    # part of the equality check below.
    report.pop("fixtures", None)
    throughput = report.get("metrics", {}).get("throughput")
    if throughput is not None:
        throughput.pop("wall_clock_seconds", None)
        throughput.pop("records_per_second", None)
    return report


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_a_live_run_at_the_committed_params_matches_the_frozen_benchmark_exactly():
    """The load-bearing test: seed=42/records=2000/months=3/engine=full is
    exactly what produced the committed freeze_2000.json. If the live
    endpoint and the CLI ever diverge, this catches it immediately."""
    response = client.post("/api/runs", json={"engine": "full", "records": 2000, "seed": 42, "months": 3})
    assert response.status_code == 200
    live = _strip_volatile(response.json())
    frozen = _strip_volatile(json.loads(FREEZE_2000.read_text(encoding="utf-8")))
    assert live == frozen


def test_a_live_run_reuses_the_real_committed_agent_traces():
    """The dashboard's whole "click an exception, see a live agent trace"
    story depends on this: because the live run reproduces run_2000 byte for
    byte at seed=42, its UNEXPLAINED_VARIANCE exceptions must carry the exact
    same trace_file links as the already-committed freeze_2000.json - not
    placeholders, not empty, the real thing."""
    response = client.post("/api/runs", json={"engine": "full", "records": 2000, "seed": 42, "months": 3})
    live_exceptions = response.json()["exceptions_detail"]
    frozen_exceptions = json.loads(FREEZE_2000.read_text(encoding="utf-8"))["exceptions_detail"]

    def trace_files_by_category(exceptions):
        return sorted(e["trace_file"] for e in exceptions if e["category"] == "UNEXPLAINED_VARIANCE")

    live_traces = trace_files_by_category(live_exceptions)
    assert live_traces, "the live run should reproduce run_2000's UNEXPLAINED_VARIANCE residual"
    assert all(t and t.startswith("sample_traces_live/") for t in live_traces)
    assert live_traces == trace_files_by_category(frozen_exceptions)


@pytest.mark.parametrize("bad_engine", ["llm-only", "not-a-real-engine", ""])
def test_the_costed_engine_is_never_reachable_from_the_api(bad_engine):
    """The safety invariant, tested directly rather than only implied by the
    allowlist's own existence. llm-only real-costs money and is explicitly
    excluded from anything a request can trigger - see api/runs.py."""
    response = client.post("/api/runs", json={"engine": bad_engine, "records": 500, "seed": 1, "months": 1})
    assert response.status_code == 400
    assert "llm-only" not in ENGINE_ALLOWLIST


@pytest.mark.parametrize("bad_records", [10, -1, 0, 50_001])
def test_records_outside_the_benchmarked_range_is_rejected(bad_records):
    response = client.post("/api/runs", json={"engine": "null", "records": bad_records, "seed": 1, "months": 1})
    assert response.status_code == 400


def test_list_benchmarks_returns_only_known_committed_names():
    response = client.get("/api/benchmarks")
    assert response.status_code == 200
    names = response.json()["benchmarks"]
    assert names, "at least the committed freeze/phase benchmarks should be listed"
    assert set(names) <= {"freeze_500", "freeze_2000", "freeze_10000", "phase3", "phase4", "phase5", "ablation_llm_only"}


def test_get_a_known_benchmark_matches_the_file_on_disk():
    response = client.get("/api/benchmarks/freeze_2000")
    assert response.status_code == 200
    assert response.json() == json.loads(FREEZE_2000.read_text(encoding="utf-8"))


@pytest.mark.parametrize("traversal_attempt", ["..%2F..%2Fpyproject", "..", "nonexistent_benchmark"])
def test_an_unlisted_or_path_traversal_name_is_rejected_not_read(traversal_attempt):
    """Never a directory listing or a path built from request input - the
    allowlist is checked first, so a traversal attempt 404s exactly like any
    other unknown name rather than reading an arbitrary file."""
    response = client.get(f"/api/benchmarks/{traversal_attempt}")
    assert response.status_code == 404
