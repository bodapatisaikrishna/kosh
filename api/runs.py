"""In-process run orchestration for the API's one genuinely new capability:
a live-triggered run, not just a served-from-disk historical result.

Deliberately thin. `run_eval` already IS the complete "generate, run, score"
pipeline the CLI (`eval/report.py`'s `main`) and `scripts/multiseed.py` both
call - this module only adds a temp-directory lifecycle around it (generate
to a scratch dir, score, delete the dir), the same pattern already used and
user-approved in `scripts/multiseed.py`, so a live-triggered run never
touches - and can never overwrite - the committed `data/fixtures/` or
`benchmarks/` state.

Never accepts an LLM client from a request: every engine here runs with
`client=None` via `eval.report.run_eval`, the same invariant the CLI already
enforces so a routine call can never spend real money. See ENGINE_ALLOWLIST.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

from data.generator.generate import run as generate_dataset
from eval.report import ENGINES, run_eval

# The live-run endpoint may never trigger "llm-only" (all-LLM, real API calls,
# real cost - see engine/pipeline.py::run_llm_only's own docstring) or any
# future engine that isn't purely deterministic. Explicit allowlist, not
# "everything except llm-only" - a new costed engine added to ENGINES later
# must be deliberately opted in here, not accidentally exposed.
ENGINE_ALLOWLIST = ("null", "oracle", "l0l1", "l0l1l2", "full")

MIN_RECORDS = 50
MAX_RECORDS = 10_000  # the largest scale already benchmarked (freeze_10000)
MIN_MONTHS = 1
MAX_MONTHS = 12

# A fixed "today" for the generator, matching every committed fixture's own
# manifest.json (see data/fixtures/*/manifest.json) - the live-run endpoint
# reproduces the committed benchmarks exactly at the same params, rather than
# drifting month to month as real wall-clock "today" advances.
GENERATOR_END_DATE = date(2026, 8, 31)


class InvalidRunRequest(ValueError):
    """Raised for a request outside the allowed engine/records/months range -
    the route layer turns this into a 400, never a 500."""


def run_live(*, engine: str, records: int, seed: int, months: int) -> dict:
    if engine not in ENGINE_ALLOWLIST:
        raise InvalidRunRequest(f"engine must be one of {ENGINE_ALLOWLIST}, got {engine!r}")
    if not (MIN_RECORDS <= records <= MAX_RECORDS):
        raise InvalidRunRequest(f"records must be between {MIN_RECORDS} and {MAX_RECORDS}, got {records}")
    if not (MIN_MONTHS <= months <= MAX_MONTHS):
        raise InvalidRunRequest(f"months must be between {MIN_MONTHS} and {MAX_MONTHS}, got {months}")

    # The fixture directory's own NAME, not its content, is what
    # eval.report.run_eval's trace-drilldown lookup keys off
    # (SAMPLE_TRACE_SOURCES matches on fixtures_dir.name == "run_2000") - so
    # the scratch directory is deliberately given the same conventional name
    # (run_<records>) a real fixture at this scale would have, not a random
    # tempfile name. Content is genuinely reproducible by seed (see the
    # module docstring), so this makes a live seed=42/records=2000 run
    # resolve to the exact same real committed agent traces as run_2000
    # itself, not silently fail to link anything.
    scratch_root = Path(tempfile.mkdtemp(prefix="kosh_api_run_"))
    tmp_dir = scratch_root / f"run_{records}"
    try:
        generate_dataset(records=records, seed=seed, months=months, end_date=GENERATOR_END_DATE, out_dir=tmp_dir)
        return run_eval(tmp_dir, engine, seed=seed)
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)


assert set(ENGINE_ALLOWLIST) <= set(ENGINES), "ENGINE_ALLOWLIST references an engine eval.report doesn't know"
assert "llm-only" not in ENGINE_ALLOWLIST, "the live-run endpoint must never be able to trigger a costed engine"
