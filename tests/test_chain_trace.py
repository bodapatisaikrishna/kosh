from datetime import date
from pathlib import Path

from data.generator.generate import run
from data.generator.trace import load_fixtures, pick_clean_bank_txn, trace


def test_pick_clean_chain_ties_out(tmp_path):
    out = tmp_path / "run"
    run(records=2000, seed=42, months=3, end_date=date(2026, 8, 31), out_dir=out)
    orders, payments, settlements, bank, ground_truth = load_fixtures(out)
    bank_txn_id = pick_clean_bank_txn(bank, settlements, ground_truth)
    output = trace(out, bank_txn_id)
    assert "RESULT: TIES OUT" in output
