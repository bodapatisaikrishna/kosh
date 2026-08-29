"""Loads the four operational CSVs into lightweight typed rows.

This module deliberately does NOT read ground_truth.json. That file exists so the
eval harness can score an engine after the fact - an engine that read it would be
cheating, and the oracle baseline (which does read it, for a very different reason:
it exists to prove the harness reports ~100% when given the true answer) lives in
eval-facing code instead, not here.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OrderRow:
    order_id: str
    order_date: str
    customer_id: str
    gross_paise: int
    currency: str
    intended_method: str
    status: str
    invoice_no: str


@dataclass(frozen=True)
class PaymentRow:
    payment_id: str
    order_id: str
    captured_at: str
    method: str
    international: bool
    gross_paise: int
    fee_paise: int
    gst_paise: int
    net_paise: int
    status: str
    settlement_id: str | None
    refund_id: str | None
    refund_paise: int


@dataclass(frozen=True)
class SettlementRow:
    settlement_id: str
    settled_at: str
    utr: str
    num_payments: int
    gross_paise: int
    fee_paise: int
    gst_paise: int
    adjustment_paise: int
    net_paise: int


@dataclass(frozen=True)
class BankRow:
    bank_txn_id: str
    value_date: str
    narration: str
    credit_paise: int
    debit_paise: int
    balance_paise: int


@dataclass
class Dataset:
    orders: list[OrderRow]
    payments: list[PaymentRow]
    settlements: list[SettlementRow]
    bank: list[BankRow]


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_dataset(fixtures_dir: Path) -> Dataset:
    orders = [
        OrderRow(
            order_id=r["order_id"],
            order_date=r["order_date"],
            customer_id=r["customer_id"],
            gross_paise=int(r["gross_paise"]),
            currency=r["currency"],
            intended_method=r["intended_method"],
            status=r["status"],
            invoice_no=r["invoice_no"],
        )
        for r in _rows(fixtures_dir / "orders.csv")
    ]
    payments = [
        PaymentRow(
            payment_id=r["payment_id"],
            order_id=r["order_id"],
            captured_at=r["captured_at"],
            method=r["method"],
            international=r["international"] == "True",
            gross_paise=int(r["gross_paise"]),
            fee_paise=int(r["fee_paise"]),
            gst_paise=int(r["gst_paise"]),
            net_paise=int(r["net_paise"]),
            status=r["status"],
            settlement_id=r["settlement_id"] or None,
            refund_id=r["refund_id"] or None,
            refund_paise=int(r["refund_paise"]),
        )
        for r in _rows(fixtures_dir / "pg_payments.csv")
    ]
    settlements = [
        SettlementRow(
            settlement_id=r["settlement_id"],
            settled_at=r["settled_at"],
            utr=r["utr"],
            num_payments=int(r["num_payments"]),
            gross_paise=int(r["gross_paise"]),
            fee_paise=int(r["fee_paise"]),
            gst_paise=int(r["gst_paise"]),
            adjustment_paise=int(r["adjustment_paise"]),
            net_paise=int(r["net_paise"]),
        )
        for r in _rows(fixtures_dir / "pg_settlements.csv")
    ]
    bank = [
        BankRow(
            bank_txn_id=r["bank_txn_id"],
            value_date=r["value_date"],
            narration=r["narration"],
            credit_paise=int(r["credit_paise"]),
            debit_paise=int(r["debit_paise"]),
            balance_paise=int(r["balance_paise"]),
        )
        for r in _rows(fixtures_dir / "bank_statement.csv")
    ]
    return Dataset(orders=orders, payments=payments, settlements=settlements, bank=bank)
