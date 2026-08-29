"""CLI entrypoint.

    python -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/

Deterministic given --seed: two runs with identical arguments produce byte-identical
files (see tests/test_generator.py). Dates anchor to --end-date, which defaults to a
fixed constant rather than today - a generator whose output depends on wall-clock time
cannot be byte-identical across runs made on different days.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from .defects import DEFECT_RATES_PER_1000, inject_all
from .emit import emit
from .world import build_clean_world

DEFAULT_END_DATE = date(2026, 8, 31)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a synthetic three-way reconciliation dataset.")
    parser.add_argument("--records", type=int, default=2000, help="number of orders to generate")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--months", type=int, default=3, help="width of the generation window, in ~30-day months")
    parser.add_argument("--end-date", type=str, default=DEFAULT_END_DATE.isoformat(), help="last order date, YYYY-MM-DD")
    parser.add_argument("--out", type=str, required=True, help="output directory")
    parser.add_argument("--defect-seed", type=int, default=None, help="defaults to --seed if omitted")
    return parser.parse_args(argv)


def run(records: int, seed: int, months: int, end_date: date, out_dir: Path, defect_seed: int | None = None) -> None:
    world = build_clean_world(records=records, seed=seed, months=months, end_date=end_date)
    defect_log = inject_all(world, seed=defect_seed if defect_seed is not None else seed, rates_per_1000=DEFECT_RATES_PER_1000)
    emit(world, defect_log, out_dir)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    end_date = date.fromisoformat(args.end_date)
    run(
        records=args.records,
        seed=args.seed,
        months=args.months,
        end_date=end_date,
        out_dir=Path(args.out),
        defect_seed=args.defect_seed,
    )
    print(f"wrote dataset to {args.out} (records={args.records}, seed={args.seed})")


if __name__ == "__main__":
    main()
