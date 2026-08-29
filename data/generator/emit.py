"""Serializes a World + DefectLog to the four schema CSVs plus ground_truth.json and
manifest.json.

CSV writing uses the stdlib csv module (not pandas) so there is zero risk of a money
column silently round-tripping through a float formatter. JSON is written with
sort_keys=True so byte-identical output does not depend on dict insertion order.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .defects import DefectLog
from .world import World

ORDER_FIELDS = ["order_id", "order_date", "customer_id", "gross_paise", "currency", "intended_method", "status", "invoice_no"]
PAYMENT_FIELDS = [
    "payment_id", "order_id", "captured_at", "method", "international",
    "gross_paise", "fee_paise", "gst_paise", "net_paise", "status",
    "settlement_id", "refund_id", "refund_paise",
]
SETTLEMENT_FIELDS = [
    "settlement_id", "settled_at", "utr", "num_payments",
    "gross_paise", "fee_paise", "gst_paise", "adjustment_paise", "net_paise",
]
BANK_FIELDS = ["bank_txn_id", "value_date", "narration", "credit_paise", "debit_paise", "balance_paise"]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_json(path: Path, obj) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True, indent=2, ensure_ascii=False)
        fh.write("\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def emit(world: World, defect_log: DefectLog, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    orders_sorted = sorted(world.orders, key=lambda o: o.order_id)
    payments_sorted = sorted(world.payments, key=lambda p: p.payment_id)
    settlements_sorted = sorted(world.settlements, key=lambda s: s.settlement_id)
    bank_sorted = sorted(world.bank_txns, key=lambda t: (t.value_date, t.bank_txn_id))

    order_rows = [
        {
            "order_id": o.order_id,
            "order_date": o.order_date.isoformat(),
            "customer_id": o.customer_id,
            "gross_paise": o.gross_paise,
            "currency": o.currency,
            "intended_method": o.intended_method,
            "status": o.status,
            "invoice_no": o.invoice_no,
        }
        for o in orders_sorted
    ]
    payment_rows = [
        {
            "payment_id": p.payment_id,
            "order_id": p.order_id,
            "captured_at": p.captured_at.isoformat() if p.captured_at else "",
            "method": p.method,
            "international": p.international,
            "gross_paise": p.gross_paise,
            "fee_paise": p.fee_paise,
            "gst_paise": p.gst_paise,
            "net_paise": p.net_paise,
            "status": p.status,
            "settlement_id": p.settlement_id or "",
            "refund_id": p.refund_id or "",
            "refund_paise": p.refund_paise,
        }
        for p in payments_sorted
    ]
    settlement_rows = [
        {
            "settlement_id": s.settlement_id,
            "settled_at": s.settled_at.isoformat(),
            "utr": s.utr,
            "num_payments": s.num_payments,
            "gross_paise": s.gross_paise,
            "fee_paise": s.fee_paise,
            "gst_paise": s.gst_paise,
            "adjustment_paise": s.adjustment_paise,
            "net_paise": s.net_paise,
        }
        for s in settlements_sorted
    ]
    bank_rows = [
        {
            "bank_txn_id": t.bank_txn_id,
            "value_date": t.value_date.isoformat(),
            "narration": t.narration,
            "credit_paise": t.credit_paise,
            "debit_paise": t.debit_paise,
            "balance_paise": t.balance_paise,
        }
        for t in bank_sorted
    ]

    _write_csv(out_dir / "orders.csv", ORDER_FIELDS, order_rows)
    _write_csv(out_dir / "pg_payments.csv", PAYMENT_FIELDS, payment_rows)
    _write_csv(out_dir / "pg_settlements.csv", SETTLEMENT_FIELDS, settlement_rows)
    _write_csv(out_dir / "bank_statement.csv", BANK_FIELDS, bank_rows)

    # --- ground truth: the true link graph + labelled defects ---------------
    order_to_payment = sorted(
        [{"order_id": p.order_id, "payment_id": p.payment_id} for p in payments_sorted],
        key=lambda r: (r["order_id"], r["payment_id"]),
    )
    payment_to_settlement = sorted(
        [
            {"payment_id": p.payment_id, "settlement_id": p.settlement_id}
            for p in payments_sorted
            if p.settlement_id
        ],
        key=lambda r: (r["settlement_id"], r["payment_id"]),
    )
    settlement_to_bank_txn = sorted(
        [
            {"settlement_id": t.settlement_id, "bank_txn_id": t.bank_txn_id}
            for t in bank_sorted
            if t.settlement_id
        ],
        key=lambda r: (r["settlement_id"], r["bank_txn_id"]),
    )

    unmatched_payment_ids = sorted(p.payment_id for p in payments_sorted if p.status == "captured" and not p.settlement_id)
    unmatched_bank_txn_ids = sorted(t.bank_txn_id for t in bank_sorted if t.kind != "settlement_credit")

    ground_truth = {
        "schema_version": 1,
        "generator": {
            "seed": world.seed,
            "records": len(world.orders),
            "months": world.months,
            "end_date": world.end_date.isoformat(),
        },
        "links": {
            "order_to_payment": order_to_payment,
            "payment_to_settlement": payment_to_settlement,
            "settlement_to_bank_txn": settlement_to_bank_txn,
        },
        "defects": [
            {
                "defect_id": d.defect_id,
                "type": d.type,
                "affected": d.affected,
                "expected_exception_category": d.expected_exception_category,
                "amount_at_risk_paise": d.amount_at_risk_paise,
                "resolvable_by_engine": d.resolvable_by_engine,
            }
            for d in sorted(defect_log.defects, key=lambda d: d.defect_id)
        ],
        "unmatched_by_design": {
            "payment_ids": unmatched_payment_ids,
            "bank_txn_ids": unmatched_bank_txn_ids,
        },
    }
    _write_json(out_dir / "ground_truth.json", ground_truth)

    # --- manifest -------------------------------------------------------------
    defect_counts: dict[str, int] = {}
    for d in defect_log.defects:
        defect_counts[d.type] = defect_counts.get(d.type, 0) + 1

    manifest = {
        "generator": {
            "seed": world.seed,
            "records": len(world.orders),
            "months": world.months,
            "end_date": world.end_date.isoformat(),
        },
        "row_counts": {
            "orders": len(order_rows),
            "pg_payments": len(payment_rows),
            "pg_settlements": len(settlement_rows),
            "bank_statement": len(bank_rows),
        },
        "totals": {
            "gmv_paise": sum(o["gross_paise"] for o in order_rows),
            "settled_net_paise": sum(s["net_paise"] for s in settlement_rows),
            "bank_credit_paise": sum(t["credit_paise"] for t in bank_rows),
            "bank_debit_paise": sum(t["debit_paise"] for t in bank_rows),
        },
        "defect_counts": defect_counts,
        "defect_total": len(defect_log.defects),
        "defect_rate_pct_of_orders": round_pct(len(defect_log.defects), len(order_rows)),
        "files": {},
    }
    _write_json(out_dir / "manifest.json", manifest)

    # sha256 the four CSVs + ground_truth.json into the manifest, then rewrite once more
    # so the manifest itself documents exactly what was shipped alongside it.
    manifest["files"] = {
        name: _sha256(out_dir / name)
        for name in ("orders.csv", "pg_payments.csv", "pg_settlements.csv", "bank_statement.csv", "ground_truth.json")
    }
    _write_json(out_dir / "manifest.json", manifest)


def round_pct(numerator: int, denominator: int) -> str:
    """Integer-only percentage string for the manifest (e.g. "9.4") - no float formatting
    surprises, just an int-tenths computation rendered as text."""
    if denominator == 0:
        return "0.0"
    tenths = (numerator * 1000) // denominator
    return f"{tenths // 10}.{tenths % 10}"
