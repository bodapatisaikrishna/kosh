import csv
import hashlib
import json
import re
from pathlib import Path

import pytest

from data.generator.generate import run
from datetime import date

SEED = 42
RECORDS = 2000
MONTHS = 3
END_DATE = date(2026, 8, 31)

AMOUNT_RE = re.compile(r"^-?\d+$")


def _generate(tmp_path: Path, suffix: str = "") -> Path:
    out = tmp_path / f"run{suffix}"
    run(records=RECORDS, seed=SEED, months=MONTHS, end_date=END_DATE, out_dir=out)
    return out


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def fixtures_dir(tmp_path_factory) -> Path:
    tmp_path = tmp_path_factory.mktemp("kosh_gen")
    return _generate(tmp_path)


@pytest.fixture(scope="module")
def data(fixtures_dir):
    orders = _read_csv(fixtures_dir / "orders.csv")
    payments = _read_csv(fixtures_dir / "pg_payments.csv")
    settlements = _read_csv(fixtures_dir / "pg_settlements.csv")
    bank = _read_csv(fixtures_dir / "bank_statement.csv")
    ground_truth = json.loads((fixtures_dir / "ground_truth.json").read_text(encoding="utf-8"))
    manifest = json.loads((fixtures_dir / "manifest.json").read_text(encoding="utf-8"))
    return dict(orders=orders, payments=payments, settlements=settlements, bank=bank,
                ground_truth=ground_truth, manifest=manifest)


# --- determinism -------------------------------------------------------------

def test_byte_identical_across_runs(tmp_path):
    run_a = tmp_path / "a"
    run_b = tmp_path / "b"
    run(records=RECORDS, seed=SEED, months=MONTHS, end_date=END_DATE, out_dir=run_a)
    run(records=RECORDS, seed=SEED, months=MONTHS, end_date=END_DATE, out_dir=run_b)
    for name in ("orders.csv", "pg_payments.csv", "pg_settlements.csv", "bank_statement.csv", "ground_truth.json", "manifest.json"):
        digest_a = hashlib.sha256((run_a / name).read_bytes()).hexdigest()
        digest_b = hashlib.sha256((run_b / name).read_bytes()).hexdigest()
        assert digest_a == digest_b, f"{name} differs between two runs with the same seed"


# --- integer-only amounts -----------------------------------------------------

def test_all_amount_columns_are_integers(data):
    money_columns = {
        "orders": ["gross_paise"],
        "payments": ["gross_paise", "fee_paise", "gst_paise", "net_paise", "refund_paise"],
        "settlements": ["gross_paise", "fee_paise", "gst_paise", "adjustment_paise", "net_paise"],
        "bank": ["credit_paise", "debit_paise", "balance_paise"],
    }
    for table, columns in money_columns.items():
        for row in data[table]:
            for col in columns:
                assert AMOUNT_RE.match(row[col]), f"{table}.{col} = {row[col]!r} is not an integer"


# --- settlement invariant (non-defective settlements only) -------------------

def test_settlement_invariant_holds_for_non_defective_settlements(data):
    dirty_settlement_ids = set()
    for d in data["ground_truth"]["defects"]:
        sid = d["affected"].get("settlement_id")
        if sid:
            dirty_settlement_ids.add(sid)

    payments_by_settlement: dict[str, list[dict]] = {}
    for p in data["payments"]:
        if p["settlement_id"]:
            payments_by_settlement.setdefault(p["settlement_id"], []).append(p)

    checked = 0
    for s in data["settlements"]:
        if s["settlement_id"] in dirty_settlement_ids:
            continue
        pays = payments_by_settlement.get(s["settlement_id"], [])
        total = sum(int(p["net_paise"]) - int(p["refund_paise"]) for p in pays) + int(s["adjustment_paise"])
        assert total == int(s["net_paise"]), f"settlement {s['settlement_id']} off by {total - int(s['net_paise'])}"
        checked += 1
    assert checked > 0


# --- referential integrity ----------------------------------------------------

def test_every_payment_order_id_exists(data):
    order_ids = {o["order_id"] for o in data["orders"]}
    for p in data["payments"]:
        assert p["order_id"] in order_ids


def test_every_nonnull_settlement_id_exists(data):
    settlement_ids = {s["settlement_id"] for s in data["settlements"]}
    for p in data["payments"]:
        if p["settlement_id"]:
            assert p["settlement_id"] in settlement_ids


def test_settlement_utrs_traceable_to_bank_except_by_design(data):
    dirty_bank_txn_ids = set()
    for d in data["ground_truth"]["defects"]:
        for k, v in d["affected"].items():
            if "bank_txn" in k:
                dirty_bank_txn_ids.add(v)

    linked_bank_txn_ids = {l["bank_txn_id"] for l in data["ground_truth"]["links"]["settlement_to_bank_txn"]}
    bank_by_id = {b["bank_txn_id"]: b for b in data["bank"]}
    for txn_id in linked_bank_txn_ids:
        if txn_id in dirty_bank_txn_ids:
            continue  # utr_mangled / settlement_split legs are allowed to not carry a clean UTR
        settlement_id = next(
            l["settlement_id"] for l in data["ground_truth"]["links"]["settlement_to_bank_txn"] if l["bank_txn_id"] == txn_id
        )
        settlement = next(s for s in data["settlements"] if s["settlement_id"] == settlement_id)
        assert settlement["utr"] in bank_by_id[txn_id]["narration"]


# --- bank ledger continuity ---------------------------------------------------

def test_bank_balance_continuity(data):
    rows = sorted(data["bank"], key=lambda r: (r["value_date"], r["bank_txn_id"]))
    assert rows, "no bank rows to check"
    first = rows[0]
    # Infer the opening balance from row 0, then replay every row's own delta
    # (including row 0's) against it - checking row 0 against its own inferred
    # opening is tautological, but every row after that is a real check.
    running = int(first["balance_paise"]) - int(first["credit_paise"]) + int(first["debit_paise"])
    for row in rows:
        running += int(row["credit_paise"]) - int(row["debit_paise"])
        assert running == int(row["balance_paise"]), f"balance discontinuity at {row['bank_txn_id']}"


# --- defect coverage -----------------------------------------------------------

def test_all_defect_types_present(data):
    expected_types = {
        "missing_settlement", "duplicate_payment", "rounding_drift", "fee_mismatch_wrong_tier",
        "gst_variance", "refund_misallocation", "orphan_chargeback", "period_cutoff",
        "utr_mangled", "fx_variance", "unidentified_credit", "settlement_split",
        # The two that make L2 and L3 genuinely necessary rather than decorative:
        "consolidated_payout",
        "compound_fee_tax_error",
    }
    present_types = {d["type"] for d in data["ground_truth"]["defects"]}
    missing = expected_types - present_types
    assert not missing, f"defect types never triggered at N={RECORDS}: {missing}"


def test_defect_rate_is_roughly_ten_percent(data):
    total_defects = len(data["ground_truth"]["defects"])
    pct = (total_defects * 100) / len(data["orders"])
    assert 5 <= pct <= 15, f"defect rate {pct:.1f}% of orders is outside the ~10% target band"


def test_manifest_gmv_matches_orders(data):
    assert data["manifest"]["totals"]["gmv_paise"] == sum(int(o["gross_paise"]) for o in data["orders"])


def test_manifest_defect_total_matches_ground_truth(data):
    assert data["manifest"]["defect_total"] == len(data["ground_truth"]["defects"])


# --- chargeback dispute-ref linkage (Phase 5 fix) -----------------------------

def test_legitimate_chargeback_narration_embeds_a_recoverable_dispute_ref(data):
    """A legitimate chargeback's narration must carry a reference the engine can
    independently derive from the payment_id alone - this is the whole point of
    the fix: without it, no engine could ever tell a legitimate chargeback apart
    from an orphan one."""
    from data.generator.ids import derive_dispute_ref

    links = data["ground_truth"]["links"]["chargeback_to_payment"]
    assert links, "expected at least one legitimate chargeback in the reference fixture"

    bank_by_id = {row["bank_txn_id"]: row for row in data["bank"]}
    for link in links:
        narration = bank_by_id[link["bank_txn_id"]]["narration"]
        expected_ref = derive_dispute_ref(link["payment_id"])
        assert expected_ref in narration.upper(), (
            f"derive_dispute_ref({link['payment_id']!r}) = {expected_ref!r} not found in "
            f"narration {narration!r}"
        )


def test_orphan_chargeback_dispute_ref_never_collides_with_a_real_payment(data):
    """An orphan chargeback's narration is deliberately the same *shape* as a
    legitimate one, but must never accidentally resolve to a real payment_id."""
    from data.generator.ids import derive_dispute_ref
    from engine.normalize import extract_dispute_ref

    real_refs = {derive_dispute_ref(p["payment_id"]) for p in data["payments"]}
    linked_txn_ids = {link["bank_txn_id"] for link in data["ground_truth"]["links"]["chargeback_to_payment"]}
    orphan_defect_txn_ids = {
        d["affected"]["bank_txn_id"] for d in data["ground_truth"]["defects"] if d["type"] == "orphan_chargeback"
    }
    assert orphan_defect_txn_ids, "expected at least one orphan_chargeback defect in the reference fixture"

    bank_by_id = {row["bank_txn_id"]: row for row in data["bank"]}
    for txn_id in orphan_defect_txn_ids:
        assert txn_id not in linked_txn_ids
        ref = extract_dispute_ref(bank_by_id[txn_id]["narration"])
        assert ref is None or ref not in real_refs


def test_unmatched_by_design_excludes_legitimate_chargebacks(data):
    linked_txn_ids = {link["bank_txn_id"] for link in data["ground_truth"]["links"]["chargeback_to_payment"]}
    unmatched = set(data["ground_truth"]["unmatched_by_design"]["bank_txn_ids"])
    assert not (linked_txn_ids & unmatched), "a legitimately-linked chargeback must not also be listed as unmatched"


# --- bank-credit adjustment never goes negative ------------------------------

def test_settlement_net_adjustment_never_drives_a_bank_credit_negative():
    """A large negative net delta applied to an already-small settlement credit
    must floor the bank credit at 0, not push it below zero - a negative bank
    credit isn't a real thing (money moving the other way is a debit). Regression
    for the multiseed sweep finding of a real `credit_paise: -34800` bank row
    (scripts/multiseed.py; surfaced at seed=100, still reproducible at seed=1
    after the intervening hardening commits shifted the RNG landscape).
    """
    from data.generator.defects import _adjust_bank_credit_for_settlement
    from data.generator.world import BankTxn, Settlement, World

    world = World(seed=0, end_date=date(2026, 8, 31), months=1)
    world.settlements.append(Settlement(settlement_id="setl_x", settled_at=date(2026, 8, 1), utr="UTR-X", net_paise=500))
    world.bank_txns.append(BankTxn(
        bank_txn_id="bank_x",
        value_date=date(2026, 8, 2),
        narration="NEFT SETL setl_x",
        credit_paise=500,
        debit_paise=0,
        balance_paise=0,
        settlement_id="setl_x",
    ))

    _adjust_bank_credit_for_settlement(world, "setl_x", delta_paise=-35_300)

    assert world.bank_txns[0].credit_paise == 0, "bank credit must floor at 0, never go negative"
