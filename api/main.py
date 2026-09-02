"""Kosh's FastAPI layer - the brief's own optional stretch goal, built after
the 7-task hardening sprint and its code freeze as a deliberate, dated scope
extension (see RESULTS.md, "Post-submission stretch goals").

Two things this API does, and one thing it deliberately never does:
- Triggers a real, live, deterministic-only run on demand (api.runs.run_live).
- Serves the already-committed, already-verified historical benchmarks.
- NEVER triggers a costed live-LLM run. api.runs.ENGINE_ALLOWLIST enforces
  this at the orchestration layer; nothing here accepts a client, a model
  name, or an API key from a request.

Local-only by design (see the plan this was built from): CORS is restricted
to the Next.js dev server's own origin, nothing is deployed or exposed
publicly, matching every other live-LLM safety boundary already established
in this project (eval/report.py's CLI keeps client=None for the same
accidental-spend-safety reason).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .runs import GENERATOR_END_DATE, InvalidRunRequest, run_live

app = FastAPI(title="Kosh API", description="Three-way payment settlement reconciliation - live run + historical benchmarks.")

# Next.js dev server only - see module docstring. Widening this to serve a
# publicly-reachable dashboard is a real decision (rate limits, no live-LLM
# key anywhere near this process) that was explicitly deferred, not an
# oversight - see the plan this API was built from.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

BENCHMARKS_DIR = Path("benchmarks")

# Explicit allowlist, never a directory listing or a path built from request
# input - closes off path traversal entirely rather than sanitizing it.
BENCHMARK_NAMES = ("freeze_500", "freeze_2000", "freeze_10000", "phase3", "phase4", "phase5", "ablation_llm_only")

# Read-only mounts for the trace files the dashboard's exception drill-down
# links to. Filenames served here are exactly the `trace_file` values
# eval.report.run_eval already returns (fixture-scoped - see
# eval/report.py::SAMPLE_TRACE_SOURCES) - this mount doesn't decide what
# links to what, it only serves what run_eval already decided.
if (Path("benchmarks/sample_traces_live")).is_dir():
    app.mount("/sample_traces_live", StaticFiles(directory="benchmarks/sample_traces_live"), name="sample_traces_live")
if (Path("benchmarks/sample_traces")).is_dir():
    app.mount("/sample_traces", StaticFiles(directory="benchmarks/sample_traces"), name="sample_traces")


class RunRequest(BaseModel):
    engine: str = "full"
    records: int = 2000
    seed: int = 42
    months: int = 3


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "generator_end_date": GENERATOR_END_DATE.isoformat()}


@app.post("/api/runs")
def create_run(body: RunRequest) -> dict:
    try:
        return run_live(engine=body.engine, records=body.records, seed=body.seed, months=body.months)
    except InvalidRunRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/benchmarks")
def list_benchmarks() -> dict:
    return {"benchmarks": [name for name in BENCHMARK_NAMES if (BENCHMARKS_DIR / f"{name}.json").exists()]}


@app.get("/api/benchmarks/{name}")
def get_benchmark(name: str) -> dict:
    if name not in BENCHMARK_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown benchmark {name!r}")
    path = BENCHMARKS_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"benchmark {name!r} has not been generated yet")

    return json.loads(path.read_text(encoding="utf-8"))
