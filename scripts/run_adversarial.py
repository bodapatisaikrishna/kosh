"""Runs Task 3's adversarial suite and writes benchmarks/adversarial.json.

Uses the exact same run_all_attacks() the pytest suite calls, so the
committed JSON and what the tests actually checked can never drift apart.

    python -m scripts.run_adversarial
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from tests.adversarial.attacks import run_all_attacks

OUT_PATH = Path("benchmarks/adversarial.json")


def main() -> None:
    results = run_all_attacks()
    report = {
        "note": (
            "Deliberate attacks engineered to induce a false match at each layer, not "
            "measurements of naturally-occurring data. Every outcome must be REFUSED "
            "(no wrong link asserted) or CORRECT (the one true link asserted) - a "
            "FALSE_MATCH here is a real bug, not a test to adjust. Attack f found one "
            "(a settlement double-claimed across two bank rows sharing a UTR) - fixed "
            "in engine/pipeline.py::_reconcile_settlement_credit_sums, see ARCHITECTURE.md."
        ),
        "attacks": [asdict(r) for r in results],
        "any_false_match": any(r.outcome == "FALSE_MATCH" for r in results),
    }
    OUT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for r in results:
        print(f"{r.attack}: {r.outcome} - {r.description} - {r.detail}")
    print()
    print(json.dumps({"any_false_match": report["any_false_match"]}))


if __name__ == "__main__":
    main()
