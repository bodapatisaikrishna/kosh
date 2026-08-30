"""Forward cash position: the other half of "run the books and the cash
position." Everything here is integer paise, same rule as everywhere else.

Settlement SLA reuses data.generator.calendar.settlement_date unchanged - the
same "one source of truth" pattern as engine/fees.py re-exporting
compute_expected_fee, so a calendar bug shows up as a generator-test failure,
never as a wrong forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from data.generator.calendar import settlement_date

from engine.contract import Match, ReconException
from engine.io import Dataset

INFLOW_WINDOW_DAYS = 14
STUCK_GRACE_DAYS = 1  # "past SLA + 1 day, still unsettled"


@dataclass
class ForecastResult:
    as_of_date: str
    inflow_curve: list[dict] = field(default_factory=list)  # [{"date": ..., "expected_inflow_paise": ...}, ...]
    stuck_paise: int = 0
    stuck_payment_ids: list[str] = field(default_factory=list)
    at_risk_paise: int = 0
    book_cash_paise: int = 0
    reconciled_cash_paise: int = 0
    reconciliation: dict = field(default_factory=dict)  # named components, must sum exactly to the delta


def _as_date(value: str) -> date:
    return datetime.fromisoformat(value).date() if "T" in value else date.fromisoformat(value)


def _infer_as_of_date(dataset: Dataset) -> date:
    """The latest CAPTURE date - the forecast's implicit "today". Deliberately
    not the latest settlement/bank date: those trail captures by the T+N
    settlement lag, so including them would push "today" past every unsettled
    payment's own expected settle date and make the 14-day curve trivially
    zero (everything still open would already read as overdue). A real system
    would pass this in explicitly; here it's derived so the forecast is
    reproducible from the fixture alone."""
    return max(_as_date(p.captured_at) for p in dataset.payments if p.captured_at)


def compute_inflow_curve(dataset: Dataset, as_of_date: date) -> list[dict]:
    """Sum of expected net inflow per day for the next 14 days, from unsettled
    captured payments only - a payment already settled isn't a future inflow."""
    by_day: dict[date, int] = {as_of_date + timedelta(days=i): 0 for i in range(INFLOW_WINDOW_DAYS)}
    for p in dataset.payments:
        if p.status != "captured" or p.settlement_id:
            continue
        expected = settlement_date(_as_date(p.captured_at), p.method)
        if expected in by_day:
            by_day[expected] += p.net_paise
    return [{"date": d.isoformat(), "expected_inflow_paise": amt} for d, amt in sorted(by_day.items())]


def compute_stuck(dataset: Dataset, as_of_date: date) -> tuple[int, list[str]]:
    """Captured, past its own method's SLA + grace, still unsettled - money that
    should have moved and hasn't, as opposed to a payment still legitimately
    in flight within its normal cycle."""
    stuck_total = 0
    stuck_ids = []
    for p in dataset.payments:
        if p.status != "captured" or p.settlement_id:
            continue
        expected = settlement_date(_as_date(p.captured_at), p.method)
        if as_of_date > expected + timedelta(days=STUCK_GRACE_DAYS):
            stuck_total += p.net_paise
            stuck_ids.append(p.payment_id)
    return stuck_total, sorted(stuck_ids)


def compute_cash_reconciliation(dataset: Dataset, matches: list[Match], exceptions: list[ReconException]) -> tuple[int, int, dict]:
    """book_cash (accrual: gross on every captured payment) vs reconciled_cash
    (cash basis: net bank movement) - the gap must be fully explained by named
    components, to the paisa.

    Derivation: settlement.net_paise (summed over all settlements) already
    equals book_cash minus unsettled payments' gross, minus settled payments'
    own fee/GST and refunds, plus settlement adjustments - that's just what a
    settlement IS. reconciled_cash then differs from that settlement total by
    whatever else moved actual bank cash without a settlement behind it:
    unidentified credits (add), and chargeback debits, legitimate or orphan
    (subtract). Every component below falls out of that identity directly -
    if it stops tying, one of these no longer matches its real-world cause.
    """
    captured = [p for p in dataset.payments if p.status == "captured"]
    settled = [p for p in captured if p.settlement_id]
    unsettled = [p for p in captured if not p.settlement_id]

    book_cash_paise = sum(p.gross_paise for p in captured)
    reconciled_cash_paise = sum(t.credit_paise - t.debit_paise for t in dataset.bank)

    unsettled_gross_paise = sum(p.gross_paise for p in unsettled)
    # gross - net, not fee + gst: net_paise is the source of truth for what a
    # settlement actually paid out, and it can differ from gross-fee-gst by a
    # few paise (rounding_drift is applied directly to net, never touching the
    # fee/gst fields) - using the actual gap, whatever caused it, is what
    # makes this tie exactly rather than off by a handful of paise.
    settled_gross_minus_net_paise = sum(p.gross_paise - p.net_paise for p in settled)
    settled_refunds_paise = sum(p.refund_paise for p in settled)

    # Settlement-side adjustments (gateway recoveries/credits) move actual bank
    # cash without book_cash ever knowing - a positive adjustment makes
    # reconciled_cash bigger, so it *shrinks* the book-minus-reconciled gap.
    adjustments_paise = sum(s.adjustment_paise for s in dataset.settlements)

    # A legitimate chargeback debit reduces actual bank cash with no
    # corresponding change to book_cash - found via L0's chargeback_payment
    # matches (a genuinely different bank_txn than the settlement credits).
    bank_by_id = {t.bank_txn_id: t for t in dataset.bank}
    legitimate_chargeback_paise = sum(
        bank_by_id[m.right_id].debit_paise for m in matches if m.link_type == "chargeback_payment" and m.right_id in bank_by_id
    )
    orphan_chargeback_paise = sum(e.amount_at_risk_paise for e in exceptions if e.category == "ORPHAN_CHARGEBACK")
    # An unidentified credit adds actual bank cash with no corresponding
    # payment at all - like a positive adjustment, it shrinks the gap.
    unidentified_credit_paise = sum(e.amount_at_risk_paise for e in exceptions if e.category == "UNIDENTIFIED_CREDIT")

    reconciliation = {
        "unsettled_gross_paise": unsettled_gross_paise,
        "settled_gross_minus_net_paise": settled_gross_minus_net_paise,
        "settled_refunds_paise": settled_refunds_paise,
        "adjustments_paise": -adjustments_paise,
        "legitimate_chargeback_paise": legitimate_chargeback_paise,
        "orphan_chargeback_paise": orphan_chargeback_paise,
        "unidentified_credit_paise": -unidentified_credit_paise,
    }
    return book_cash_paise, reconciled_cash_paise, reconciliation


def compute_forecast(
    dataset: Dataset, matches: list[Match], exceptions: list[ReconException], as_of_date: date | None = None
) -> ForecastResult:
    as_of = as_of_date or _infer_as_of_date(dataset)
    inflow_curve = compute_inflow_curve(dataset, as_of)
    stuck_paise, stuck_ids = compute_stuck(dataset, as_of)
    at_risk_paise = sum(e.amount_at_risk_paise for e in exceptions)
    book_cash_paise, reconciled_cash_paise, reconciliation = compute_cash_reconciliation(dataset, matches, exceptions)

    return ForecastResult(
        as_of_date=as_of.isoformat(),
        inflow_curve=inflow_curve,
        stuck_paise=stuck_paise,
        stuck_payment_ids=stuck_ids,
        at_risk_paise=at_risk_paise,
        book_cash_paise=book_cash_paise,
        reconciled_cash_paise=reconciled_cash_paise,
        reconciliation=reconciliation,
    )
