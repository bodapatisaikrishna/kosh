"""Task 5 of the hardening sprint: engine/io.py::load_dataset must fail loudly
on a malformed file, never silently skip a row or coerce a bad value.

Every case here builds a tiny, deliberately-broken CSV set in tmp_path (not a
committed fixture file) and asserts both the exception TYPE
(DatasetValidationError) and that the message names the specific file, row,
and/or field - not just that *some* exception was raised.
"""

from __future__ import annotations

from pathlib import Path

from engine.io import DatasetValidationError, load_dataset

ORDERS_HEADER = "order_id,order_date,customer_id,gross_paise,currency,intended_method,status,invoice_no"
PAYMENTS_HEADER = "payment_id,order_id,captured_at,method,international,gross_paise,fee_paise,gst_paise,net_paise,status,settlement_id,refund_id,refund_paise"
SETTLEMENTS_HEADER = "settlement_id,settled_at,utr,num_payments,gross_paise,fee_paise,gst_paise,adjustment_paise,net_paise"
BANK_HEADER = "bank_txn_id,value_date,narration,credit_paise,debit_paise,balance_paise"

ORDER_ROW = "order_1,2026-08-01,cust_1,100000,INR,upi,paid,INV-1"
PAYMENT_ROW = "pay_1,order_1,2026-08-01T10:00:00,upi,False,100000,0,0,100000,captured,setl_1,,0"
SETTLEMENT_ROW = "setl_1,2026-08-03,HDFCN00000000001,1,100000,0,0,0,100000"
BANK_ROW = "btxn_1,2026-08-03,NEFT-HDFCN00000000001-RAZORPAY SOFTWARE PVT LTD,100000,0,0"


def _write_fixtures(tmp_path: Path, *, orders=None, payments=None, settlements=None, bank=None) -> Path:
    """Writes a complete, otherwise-valid 4-file fixture set, with one file's
    content overridden by the caller to introduce exactly one malformation."""
    (tmp_path / "orders.csv").write_text(orders if orders is not None else f"{ORDERS_HEADER}\n{ORDER_ROW}\n")
    (tmp_path / "pg_payments.csv").write_text(payments if payments is not None else f"{PAYMENTS_HEADER}\n{PAYMENT_ROW}\n")
    (tmp_path / "pg_settlements.csv").write_text(settlements if settlements is not None else f"{SETTLEMENTS_HEADER}\n{SETTLEMENT_ROW}\n")
    (tmp_path / "bank_statement.csv").write_text(bank if bank is not None else f"{BANK_HEADER}\n{BANK_ROW}\n")
    return tmp_path


def _expect_error(tmp_path: Path, **overrides):
    fixtures = _write_fixtures(tmp_path, **overrides)
    try:
        load_dataset(fixtures)
        assert False, "expected DatasetValidationError"
    except DatasetValidationError as exc:
        return exc


def test_missing_required_column(tmp_path):
    broken = "order_id,order_date,customer_id,gross_paise,currency,intended_method,status\norder_1,2026-08-01,cust_1,100000,INR,upi,paid\n"
    exc = _expect_error(tmp_path, orders=broken)
    assert exc.filename == "orders.csv"
    assert "invoice_no" in str(exc)


def test_duplicate_header_row(tmp_path):
    broken = f"{ORDERS_HEADER},gross_paise\n{ORDER_ROW},100000\n"
    exc = _expect_error(tmp_path, orders=broken)
    assert exc.filename == "orders.csv"
    assert "duplicate header" in str(exc).lower()
    assert "gross_paise" in str(exc)


def test_non_integer_amount(tmp_path):
    broken_row = "pay_1,order_1,2026-08-01T10:00:00,upi,False,not-a-number,0,0,100000,captured,setl_1,,0"
    exc = _expect_error(tmp_path, payments=f"{PAYMENTS_HEADER}\n{broken_row}\n")
    assert exc.filename == "pg_payments.csv"
    assert exc.field == "gross_paise"
    assert exc.row == 2
    assert "not-a-number" in str(exc)


def test_negative_value_where_only_positive_is_valid(tmp_path):
    # An order's gross amount is never negative in the real world - unlike
    # e.g. adjustment_paise, which is a genuinely signed figure (see
    # engine/io.py's own comment on why credit_paise is NOT checked here).
    broken_row = "order_1,2026-08-01,cust_1,-100000,INR,upi,paid,INV-1"
    exc = _expect_error(tmp_path, orders=f"{ORDERS_HEADER}\n{broken_row}\n")
    assert exc.filename == "orders.csv"
    assert exc.field == "gross_paise"
    assert "negative" in str(exc).lower()


def test_unexpected_date_format(tmp_path):
    broken_row = "order_1,08/01/2026,cust_1,100000,INR,upi,paid,INV-1"
    exc = _expect_error(tmp_path, orders=f"{ORDERS_HEADER}\n{broken_row}\n")
    assert exc.filename == "orders.csv"
    assert exc.field == "order_date"


def test_empty_file(tmp_path):
    exc = _expect_error(tmp_path, orders="")
    assert exc.filename == "orders.csv"
    assert "empty" in str(exc).lower()


def test_headers_only_file(tmp_path):
    exc = _expect_error(tmp_path, orders=f"{ORDERS_HEADER}\n")
    assert exc.filename == "orders.csv"
    assert "no data rows" in str(exc).lower()


def test_non_utf8_byte_sequence(tmp_path):
    fixtures = _write_fixtures(tmp_path)
    (fixtures / "orders.csv").write_bytes(f"{ORDERS_HEADER}\n".encode("utf-8") + b"order_1,2026-08-01,cust_1,\xff\xfe,INR,upi,paid,INV-1\n")
    try:
        load_dataset(fixtures)
        assert False, "expected DatasetValidationError"
    except DatasetValidationError as exc:
        assert exc.filename == "orders.csv"
        assert "utf-8" in str(exc).lower()


def test_duplicate_primary_key(tmp_path):
    broken = f"{PAYMENTS_HEADER}\n{PAYMENT_ROW}\npay_1,order_1,2026-08-01T10:05:00,upi,False,50000,0,0,50000,captured,,,0\n"
    exc = _expect_error(tmp_path, payments=broken)
    assert exc.filename == "pg_payments.csv"
    assert exc.field == "payment_id"
    assert exc.row == 3  # the SECOND occurrence is what's rejected
    assert "duplicate" in str(exc).lower()
    assert "pay_1" in str(exc)


def test_row_with_more_fields_than_header(tmp_path):
    broken_row = f"{ORDER_ROW},EXTRA_FIELD"
    exc = _expect_error(tmp_path, orders=f"{ORDERS_HEADER}\n{broken_row}\n")
    assert exc.filename == "orders.csv"
    assert exc.row == 2
    assert "more fields" in str(exc).lower()


def test_a_clean_fixture_set_still_loads_normally(tmp_path):
    # The whole point of every case above: this must still work unchanged.
    fixtures = _write_fixtures(tmp_path)
    dataset = load_dataset(fixtures)
    assert len(dataset.orders) == 1
    assert len(dataset.payments) == 1
    assert len(dataset.settlements) == 1
    assert len(dataset.bank) == 1


def test_a_failed_payment_with_no_captured_at_is_not_a_validation_error(tmp_path):
    # Found while building this suite: a "failed" payment legitimately has an
    # empty captured_at (it never captured) - this must not be treated as a
    # malformed date, only a genuinely unparseable non-empty value should be.
    payment_row = "pay_2,order_1,,upi,False,100000,0,0,0,failed,,,0"
    fixtures = _write_fixtures(tmp_path, payments=f"{PAYMENTS_HEADER}\n{PAYMENT_ROW}\n{payment_row}\n")
    dataset = load_dataset(fixtures)
    failed = [p for p in dataset.payments if p.payment_id == "pay_2"]
    assert failed[0].captured_at == ""
