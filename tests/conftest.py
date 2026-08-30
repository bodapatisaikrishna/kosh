"""Makes the test suite self-sufficient on a fresh clone.

Four test modules score the engine against `data/fixtures/run_2000` - the
reference fixture. That fixture is deliberately gitignored (it's ~1MB of CSV
that is reproducible byte-for-byte from its seed, so committing it would just
be storing a derived artifact), which means a fresh clone doesn't have it and
`git clone && pytest` - the most natural thing a reviewer does - failed with
7 FileNotFoundErrors before this existed.

Regenerating is the honest fix rather than committing the fixture: generation
is deterministic (same seed -> byte-identical output, asserted by
test_generator.py), takes well under a second, and keeps the repo storing
source rather than output. The fixture is only built when genuinely absent, so
the normal dev loop pays nothing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# (directory, records, seed, months, end_date) - must match the parameters the
# committed benchmarks and the Makefile use, or the scored numbers would drift.
REQUIRED_FIXTURES = [
    ("run_2000", 2000, 42, 3, date(2026, 8, 31)),
]


@pytest.fixture(scope="session", autouse=True)
def ensure_reference_fixtures() -> None:
    """Generate any gitignored reference fixture the suite needs but that a
    fresh clone won't have. Autouse + session-scoped: no test needs to opt in,
    and the cost is paid at most once per run."""
    from data.generator.generate import run

    for name, records, seed, months, end_date in REQUIRED_FIXTURES:
        out_dir = REPO_ROOT / "data" / "fixtures" / name
        if (out_dir / "ground_truth.json").exists():
            continue
        run(records=records, seed=seed, months=months, end_date=end_date, out_dir=out_dir)
