"""Ground-truth loading, kept in eval/ (not engine/) on purpose: the eval harness's
whole job is to know the true answer and score an engine against it, but an engine
must never see this file - see engine/io.py's docstring.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_ground_truth(fixtures_dir: Path) -> dict:
    return json.loads((Path(fixtures_dir) / "ground_truth.json").read_text(encoding="utf-8"))
