"""Loads the four operational CSVs into lightweight typed rows.

This module deliberately does NOT read ground_truth.json. That file exists so the
eval harness can score an engine after the fact - an engine that read it would be
cheating, and the oracle baseline (which does read it, for a very different reason:
it exists to prove the harness reports ~100% when given the true answer) lives in
eval-facing code instead, not here.

Input validation (hardening sprint, Task 5): a real reconciliation system ingests
files produced by other systems - a bank export, an ERP dump - and those break in
ways a synthetic generator never will. load_dataset's public signature and return
type are unchanged (every existing call site keeps working); each of the four CSVs
now goes through a small per-file validation pass first. Never silently skips a
row or coerces a bad value - every problem is a DatasetValidationError naming the
file, the 1-indexed row (counting the header row, same as a human opening the file
in a spreadsheet would), the field, and why. Fails at the file boundary: a whole
file is cheap to fully scan, so every problem in ONE broken file is reported
together, but the very first file with any problem stops the whole load rather
than silently reading the other three and reporting a partial, confusing picture.
"""

from __future__ import annotations

import csv
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


class DatasetValidationError(Exception):
    """A malformed input file - never a bare KeyError/ValueError/UnicodeDecodeError,
    so the caller gets a specific, actionable reason rather than a raw traceback."""

    def __init__(self, filename: str, reason: str, row: int | None = None, field: str | None = None) -> None:
        self.filename = filename
        self.row = row
        self.field = field
        self.reason = reason
        location = filename
        if row is not None:
            location += f", row {row}"
        if field is not None:
            location += f", field {field!r}"
        super().__init__(f"{location}: {reason}")


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


def _rows(path: Path) -> tuple[list[str] | None, list[dict]]:
    """Returns (fieldnames, rows). fieldnames is None for a genuinely empty
    file (not even a header). Wraps decode errors into the same actionable
    exception type as every other validation failure here, rather than
    letting a raw UnicodeDecodeError propagate."""
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            return reader.fieldnames, rows
    except UnicodeDecodeError as exc:
        raise DatasetValidationError(path.name, f"not valid UTF-8 ({exc})") from exc


def _validate_header(filename: str, fieldnames: list[str] | None, expected: tuple[str, ...]) -> None:
    if fieldnames is None:
        raise DatasetValidationError(filename, "file is empty (no header row)")
    seen: set[str] = set()
    duplicates = set()
    for name in fieldnames:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise DatasetValidationError(filename, f"duplicate header column(s): {sorted(duplicates)}")
    missing = [c for c in expected if c not in seen]
    if missing:
        raise DatasetValidationError(filename, f"missing required column(s): {missing}")


def _require_data_rows(filename: str, rows: list[dict]) -> None:
    if not rows:
        raise DatasetValidationError(filename, "file has a header but no data rows")


def _check_overflow(filename: str, row_num: int, row: dict) -> None:
    # csv.DictReader puts any extra fields beyond the header count under the
    # None key, as a list - it never raises on this by itself.
    if None in row:
        raise DatasetValidationError(filename, f"row has more fields than the header ({len(row[None])} extra)", row=row_num)


def _parse_int(filename: str, row_num: int, row: dict, field: str, *, non_negative: bool = False) -> int:
    raw = row.get(field)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise DatasetValidationError(filename, f"{raw!r} is not a valid integer", row=row_num, field=field) from None
    if non_negative and value < 0:
        raise DatasetValidationError(filename, f"{value} must not be negative", row=row_num, field=field)
    return value


def _parse_date(filename: str, row_num: int, row: dict, field: str) -> str:
    # Empty is a legitimate value here, not a format error - e.g. a "failed"
    # payment never captured, so it genuinely has no captured_at at all.
    # Only a non-empty, unparseable value is a real format problem.
    raw = row.get(field, "")
    if not raw:
        return raw
    try:
        _as_date(raw)
    except (TypeError, ValueError):
        raise DatasetValidationError(filename, f"{raw!r} is not a recognised date/datetime (expected YYYY-MM-DD or ISO datetime)", row=row_num, field=field) from None
    return raw


def _check_duplicate_id(filename: str, row_num: int, row: dict, id_field: str, seen_ids: set[str]) -> str:
    value = row.get(id_field, "")
    if value in seen_ids:
        raise DatasetValidationError(filename, f"duplicate {id_field} {value!r} (first seen earlier in this file)", row=row_num, field=id_field)
    seen_ids.add(value)
    return value


def load_dataset(fixtures_dir: Path) -> Dataset:
    order_fields = tuple(f.name for f in dataclasses.fields(OrderRow))
    payment_fields = tuple(f.name for f in dataclasses.fields(PaymentRow))
    settlement_fields = tuple(f.name for f in dataclasses.fields(SettlementRow))
    bank_fields = tuple(f.name for f in dataclasses.fields(BankRow))

    orders_file = "orders.csv"
    fieldnames, rows = _rows(fixtures_dir / orders_file)
    _validate_header(orders_file, fieldnames, order_fields)
    _require_data_rows(orders_file, rows)
    seen_order_ids: set[str] = set()
    orders = []
    for i, r in enumerate(rows, start=2):  # row 1 is the header
        _check_overflow(orders_file, i, r)
        order_id = _check_duplicate_id(orders_file, i, r, "order_id", seen_order_ids)
        _parse_date(orders_file, i, r, "order_date")
        orders.append(OrderRow(
            order_id=order_id,
            order_date=r["order_date"],
            customer_id=r["customer_id"],
            gross_paise=_parse_int(orders_file, i, r, "gross_paise", non_negative=True),
            currency=r["currency"],
            intended_method=r["intended_method"],
            status=r["status"],
            invoice_no=r["invoice_no"],
        ))

    payments_file = "pg_payments.csv"
    fieldnames, rows = _rows(fixtures_dir / payments_file)
    _validate_header(payments_file, fieldnames, payment_fields)
    _require_data_rows(payments_file, rows)
    seen_payment_ids: set[str] = set()
    payments = []
    for i, r in enumerate(rows, start=2):
        _check_overflow(payments_file, i, r)
        payment_id = _check_duplicate_id(payments_file, i, r, "payment_id", seen_payment_ids)
        _parse_date(payments_file, i, r, "captured_at")
        payments.append(PaymentRow(
            payment_id=payment_id,
            order_id=r["order_id"],
            captured_at=r["captured_at"],
            method=r["method"],
            international=r["international"] == "True",
            gross_paise=_parse_int(payments_file, i, r, "gross_paise", non_negative=True),
            fee_paise=_parse_int(payments_file, i, r, "fee_paise"),
            gst_paise=_parse_int(payments_file, i, r, "gst_paise"),
            net_paise=_parse_int(payments_file, i, r, "net_paise"),
            status=r["status"],
            settlement_id=r["settlement_id"] or None,
            refund_id=r["refund_id"] or None,
            refund_paise=_parse_int(payments_file, i, r, "refund_paise"),
        ))

    settlements_file = "pg_settlements.csv"
    fieldnames, rows = _rows(fixtures_dir / settlements_file)
    _validate_header(settlements_file, fieldnames, settlement_fields)
    _require_data_rows(settlements_file, rows)
    seen_settlement_ids: set[str] = set()
    settlements = []
    for i, r in enumerate(rows, start=2):
        _check_overflow(settlements_file, i, r)
        settlement_id = _check_duplicate_id(settlements_file, i, r, "settlement_id", seen_settlement_ids)
        _parse_date(settlements_file, i, r, "settled_at")
        settlements.append(SettlementRow(
            settlement_id=settlement_id,
            settled_at=r["settled_at"],
            utr=r["utr"],
            num_payments=_parse_int(settlements_file, i, r, "num_payments"),
            gross_paise=_parse_int(settlements_file, i, r, "gross_paise", non_negative=True),
            fee_paise=_parse_int(settlements_file, i, r, "fee_paise"),
            gst_paise=_parse_int(settlements_file, i, r, "gst_paise"),
            # adjustment_paise is deliberately NOT non_negative: a settlement
            # adjustment (a gateway recovery or credit) is a real, signed
            # figure - see data/generator/world.py's own +/- adjustment.
            adjustment_paise=_parse_int(settlements_file, i, r, "adjustment_paise"),
            net_paise=_parse_int(settlements_file, i, r, "net_paise"),
        ))

    bank_file = "bank_statement.csv"
    fieldnames, rows = _rows(fixtures_dir / bank_file)
    _validate_header(bank_file, fieldnames, bank_fields)
    _require_data_rows(bank_file, rows)
    seen_bank_ids: set[str] = set()
    bank = []
    for i, r in enumerate(rows, start=2):
        _check_overflow(bank_file, i, r)
        bank_txn_id = _check_duplicate_id(bank_file, i, r, "bank_txn_id", seen_bank_ids)
        _parse_date(bank_file, i, r, "value_date")
        bank.append(BankRow(
            bank_txn_id=bank_txn_id,
            value_date=r["value_date"],
            narration=r["narration"],
            # A bank line's credit/debit amount is only ever moved in one
            # direction per column - "a negative credit" is not a real thing
            # (money that moved the other way is a debit, not a negative
            # credit), so both are validated non-negative.
            # credit_paise/debit_paise are deliberately NOT validated non-negative:
            # a defect injector can drive a bank credit negative via a large
            # negative delta applied to an already-small credit (see
            # data/generator/defects.py's settlement-net-adjustment helper) -
            # rare, empirically never seen in the four committed fixtures, but
            # confirmed to occur at at least one of the multiseed hardening
            # sprint's six seeds. Flagged as a separate generator-side finding
            # rather than papered over here; see ARCHITECTURE.md.
            credit_paise=_parse_int(bank_file, i, r, "credit_paise"),
            debit_paise=_parse_int(bank_file, i, r, "debit_paise"),
            balance_paise=_parse_int(bank_file, i, r, "balance_paise"),
        ))

    return Dataset(orders=orders, payments=payments, settlements=settlements, bank=bank)


def aging_days(as_of: date, event_date_str: str) -> int:
    """Days between some event (a payment's capture, a settlement's date, a
    bank line's value date) and "now". Clamped at 0: never negative - a
    negative value would only mean a data-quality issue in the fixture
    itself (an event dated after "now"), and no ledger should ever display
    a number that reads as "this hasn't happened yet". Shared by
    engine/exceptions.py and engine/l3_tools.py so both compute this
    identically."""
    return max(0, (as_of - _as_date(event_date_str)).days)


def infer_as_of_date(dataset: Dataset) -> date:
    """The latest CAPTURE date - the dataset's implicit "today". Shared by
    cash/forecast.py's 14-day forecast and engine/exceptions.py's aging_days,
    so both agree on what "now" means for the same fixture - one source of
    truth, same pattern as compute_expected_fee being shared between the
    generator and the engine.

    Deliberately not the latest settlement/bank date: those trail captures by
    the T+N settlement lag, so including them would push "today" past every
    unsettled payment's own expected settle date. A real system would pass
    this in explicitly (see the Limitations section on this); here it's
    derived so both are reproducible from the fixture alone.

    Falls back to the latest settlement/bank date if there are no payments at
    all (a settlement-only or bank-only dataset slice - real for an isolated
    classifier test, and not impossible for a partial real-world batch), and
    to today's real date only if the dataset carries no dates whatsoever -
    a fully empty dataset has no fixture-derived "now" to fall back to."""
    captured_dates = [_as_date(p.captured_at) for p in dataset.payments if p.captured_at]
    if captured_dates:
        return max(captured_dates)
    other_dates = [
        *(_as_date(s.settled_at) for s in dataset.settlements if s.settled_at),
        *(_as_date(b.value_date) for b in dataset.bank if b.value_date),
    ]
    return max(other_dates) if other_dates else date.today()


def _as_date(value: str) -> date:
    return datetime.fromisoformat(value).date() if "T" in value else date.fromisoformat(value)
