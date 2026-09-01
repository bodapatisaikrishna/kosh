"""Regenerate every multiseed seed and assert no bank_statement.csv row carries
a negative credit_paise (or debit_paise).

Backs the generator-side fix in data/generator/defects.py
(_adjust_bank_credit_for_settlement now floors the adjusted credit at 0). Before
that fix, seed=100 at N=2000 produced a real `credit_paise: -34800` row - see
scripts/multiseed.py for the sweep that first surfaced it.

    python -m scripts.verify_no_negative_bank_credit

Exits non-zero if any seed produces a negative credit or debit.
"""

from __future__ import annotations

import csv
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from data.generator.generate import run as generate_run
from scripts.multiseed import MONTHS, RECORDS, SEEDS

END_DATE = date(2026, 8, 31)


def _negative_rows(fixtures_dir: Path) -> list[tuple[int, str, str]]:
    bad: list[tuple[int, str, str]] = []
    with (fixtures_dir / "bank_statement.csv").open(newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            for col in ("credit_paise", "debit_paise"):
                if int(row[col]) < 0:
                    bad.append((i, col, row[col]))
    return bad


def main() -> int:
    failures: list[str] = []
    for seed in SEEDS:
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"kosh_negcredit_{seed}_"))
        try:
            generate_run(records=RECORDS, seed=seed, months=MONTHS, end_date=END_DATE, out_dir=tmp_dir)
            bad = _negative_rows(tmp_dir)
            if bad:
                for line_num, col, value in bad:
                    failures.append(f"seed {seed}: bank_statement.csv line {line_num} has {col}={value}")
            else:
                print(f"seed {seed}: OK (no negative credit/debit across {RECORDS} records)")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if failures:
        print()
        for line in failures:
            print(f"!!! {line}")
        return 1
    print()
    print(f"all {len(SEEDS)} seeds clean: no negative credit_paise/debit_paise anywhere")
    return 0


if __name__ == "__main__":
    sys.exit(main())
