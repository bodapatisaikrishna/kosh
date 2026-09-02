"""Task 6.3 of the hardening sprint: every pipeline run should be able to say
exactly what produced it - which commit, whether the tree was clean, what was
run against what. Embedded into every eval.report run's own JSON, and
rendered into the HTML report's footer, so a "frozen" benchmark that was
actually produced from a dirty working tree is visibly flagged as such
rather than silently passing for reproducible.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from engine.io import Dataset

# Only the packages this project's own code actually imports - not the full
# dependency list, and not the ones that are declared but unused (see the
# pydantic/fastapi/uvicorn finding in Task 6.4, and pandas, which was found
# later and removed for the same reason - this list itself was tracking the
# version of a package nothing imported). Both remaining entries are optional:
# a deterministic-only run has neither installed, which is why a missing
# package is skipped rather than recorded as an error.
_TRACKED_PACKAGES = ("openai", "anthropic")

_INPUT_FILES = ("orders.csv", "pg_payments.csv", "pg_settlements.csv", "bank_statement.csv")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _git_commit_sha() -> str | None:
    return _git("rev-parse", "HEAD")


def _git_is_dirty() -> bool | None:
    status = _git("status", "--porcelain")
    return None if status is None else bool(status)


def _package_versions() -> dict[str, str]:
    versions = {}
    for name in _TRACKED_PACKAGES:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            pass  # not installed - e.g. openai/anthropic without the llm extra
    return versions


def _file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_run_manifest(fixtures_dir: Path, engine_name: str, dataset: Dataset, seed: int | None = None, model_name: str | None = None) -> dict:
    """Everything needed to answer "what exactly produced this benchmark"
    after the fact: which commit, whether the tree was clean at the time,
    what was run against what, and a content hash of every input file (so a
    silently-regenerated fixture with the same filename is still detectable)."""
    fixtures_dir = Path(fixtures_dir)
    return {
        "git_commit_sha": _git_commit_sha(),
        "git_dirty": _git_is_dirty(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine": engine_name,
        "seed": seed,
        "model": model_name,
        "fixtures_dir": str(fixtures_dir),
        "record_counts": {
            "orders": len(dataset.orders),
            "payments": len(dataset.payments),
            "settlements": len(dataset.settlements),
            "bank": len(dataset.bank),
        },
        "package_versions": _package_versions(),
        "input_file_sha256": {name: _file_hash(fixtures_dir / name) for name in _INPUT_FILES},
    }
