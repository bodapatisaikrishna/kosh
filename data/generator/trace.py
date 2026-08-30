"""Hand-verification tool: bank credit -> settlement -> payments -> orders.

    python -m data.generator.trace --fixtures data/fixtures/run_2000 --bank-txn btxn_...
    python -m data.generator.trace --fixtures data/fixtures/run_2000 --pick-clean

This is the Phase 1 checkpoint gate: "you can manually trace one bank credit to its
orders, by hand, and it ties out." --pick-clean deterministically picks the first
bank credit (by bank_txn_id) whose settlement carries zero injected defects, so the
demo trace always ties to the paisa.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_fixtures(fixtures_dir: Path):
    orders = _read_csv(fixtures_dir / "orders.csv")
    payments = _read_csv(fixtures_dir / "pg_payments.csv")
    settlements = _read_csv(fixtures_dir / "pg_settlements.csv")
    bank = _read_csv(fixtures_dir / "bank_statement.csv")
    ground_truth = json.loads((fixtures_dir / "ground_truth.json").read_text(encoding="utf-8"))
    return orders, payments, settlements, bank, ground_truth


def defect_settlement_ids(ground_truth: dict) -> set[str]:
    ids: set[str] = set()
    for d in ground_truth["defects"]:
        sid = d["affected"].get("settlement_id")
        if sid:
            ids.add(sid)
        for key in ("bank_txn_id_a", "bank_txn_id_b"):
            pass  # bank_txn_ids handled separately below
    return ids


def defect_bank_txn_ids(ground_truth: dict) -> set[str]:
    ids: set[str] = set()
    for d in ground_truth["defects"]:
        for key, val in d["affected"].items():
            if "bank_txn" in key:
                ids.add(val)
    return ids


def defect_payment_ids(ground_truth: dict) -> set[str]:
    ids: set[str] = set()
    for d in ground_truth["defects"]:
        for key, val in d["affected"].items():
            if key == "payment_id" or key == "duplicate_payment_id":
                ids.add(val)
    return ids


def pick_clean_bank_txn(bank: list[dict], settlements: list[dict], ground_truth: dict) -> str:
    dirty_settlements = defect_settlement_ids(ground_truth)
    dirty_bank_txns = defect_bank_txn_ids(ground_truth)
    dirty_payments = defect_payment_ids(ground_truth)
    pay_to_settlement = {r["payment_id"]: r["settlement_id"] for r in ground_truth["links"]["payment_to_settlement"]}
    settle_to_payments: dict[str, list[str]] = {}
    for payment_id, settlement_id in pay_to_settlement.items():
        settle_to_payments.setdefault(settlement_id, []).append(payment_id)

    for txn in sorted(bank, key=lambda t: t["bank_txn_id"]):
        settlement_id = next(
            (link["settlement_id"] for link in ground_truth["links"]["settlement_to_bank_txn"] if link["bank_txn_id"] == txn["bank_txn_id"]),
            None,
        )
        if not settlement_id or settlement_id in dirty_settlements or txn["bank_txn_id"] in dirty_bank_txns:
            continue
        payment_ids = settle_to_payments.get(settlement_id, [])
        if not payment_ids or any(p in dirty_payments for p in payment_ids):
            continue
        # exactly one bank_txn per settlement for a clean chain (no split)
        linked_txns = [l["bank_txn_id"] for l in ground_truth["links"]["settlement_to_bank_txn"] if l["settlement_id"] == settlement_id]
        if len(linked_txns) != 1:
            continue
        return txn["bank_txn_id"]
    raise SystemExit("no clean bank credit found - fixtures may be too small or too noisy")


def trace(fixtures_dir: Path, bank_txn_id: str) -> str:
    orders, payments, settlements, bank, ground_truth = load_fixtures(fixtures_dir)
    orders_by_id = {o["order_id"]: o for o in orders}
    payments_by_id = {p["payment_id"]: p for p in payments}
    settlements_by_id = {s["settlement_id"]: s for s in settlements}
    bank_by_id = {b["bank_txn_id"]: b for b in bank}

    txn = bank_by_id.get(bank_txn_id)
    if txn is None:
        raise SystemExit(f"no such bank_txn_id: {bank_txn_id}")

    settlement_id = next(
        (l["settlement_id"] for l in ground_truth["links"]["settlement_to_bank_txn"] if l["bank_txn_id"] == bank_txn_id),
        None,
    )
    lines: list[str] = []
    lines.append(f"BANK CREDIT {txn['bank_txn_id']}  value_date={txn['value_date']}  credit_paise={txn['credit_paise']}")
    lines.append(f"  narration: {txn['narration']!r}")

    if settlement_id is None:
        lines.append("  -> no settlement linked (by design: customer credit / chargeback / split leg)")
        return "\n".join(lines)

    settlement = settlements_by_id[settlement_id]
    lines.append(
        f"  SETTLEMENT {settlement_id}  settled_at={settlement['settled_at']}  utr={settlement['utr']}  "
        f"num_payments={settlement['num_payments']}  net_paise={settlement['net_paise']}  "
        f"adjustment_paise={settlement['adjustment_paise']}"
    )

    payment_ids = sorted(pid for pid, sid in
                          ((l["payment_id"], l["settlement_id"]) for l in ground_truth["links"]["payment_to_settlement"])
                          if sid == settlement_id)

    payment_net_sum = 0
    for payment_id in payment_ids:
        payment = payments_by_id[payment_id]
        order = orders_by_id.get(payment["order_id"], {})
        payment_net_sum += int(payment["net_paise"]) - int(payment["refund_paise"])
        lines.append(
            f"    PAYMENT {payment_id}  order_id={payment['order_id']}  method={payment['method']}  "
            f"gross={payment['gross_paise']}  fee={payment['fee_paise']}  gst={payment['gst_paise']}  "
            f"net={payment['net_paise']}  refund={payment['refund_paise']}"
        )
        lines.append(
            f"      ORDER {order.get('order_id', '?')}  gross_paise={order.get('gross_paise', '?')}  "
            f"invoice_no={order.get('invoice_no', '?')}"
        )

    other_credits = [
        l["bank_txn_id"] for l in ground_truth["links"]["settlement_to_bank_txn"] if l["settlement_id"] == settlement_id
    ]
    all_credits_sum = sum(int(bank_by_id[bid]["credit_paise"]) for bid in other_credits)

    expected_net = payment_net_sum + int(settlement["adjustment_paise"])
    lines.append(f"  SUBTOTAL sum(payment net - refund) + adjustment = {expected_net}")
    lines.append(f"  SETTLEMENT.net_paise                            = {settlement['net_paise']}")
    lines.append(f"  SUM of bank credit(s) for this settlement        = {all_credits_sum}")

    off_by_settlement = expected_net - int(settlement["net_paise"])
    off_by_bank = all_credits_sum - int(settlement["net_paise"])
    if off_by_settlement == 0 and off_by_bank == 0:
        lines.append("  RESULT: TIES OUT")
    else:
        lines.append(f"  RESULT: OFF BY {off_by_settlement} paise (payments vs settlement), {off_by_bank} paise (bank vs settlement)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Trace one bank credit to its settlement, payments and orders.")
    parser.add_argument("--fixtures", required=True, type=str)
    parser.add_argument("--bank-txn", type=str, default=None)
    parser.add_argument("--pick-clean", action="store_true", help="deterministically pick a defect-free chain")
    args = parser.parse_args(argv)

    fixtures_dir = Path(args.fixtures)
    if args.pick_clean:
        orders, payments, settlements, bank, ground_truth = load_fixtures(fixtures_dir)
        bank_txn_id = pick_clean_bank_txn(bank, settlements, ground_truth)
    elif args.bank_txn:
        bank_txn_id = args.bank_txn
    else:
        raise SystemExit("pass --bank-txn <id> or --pick-clean")

    print(trace(fixtures_dir, bank_txn_id))


if __name__ == "__main__":
    main()
