"""Builds one internally-consistent, defect-free "world": orders, payments, refunds,
settlements and a bank statement that all tie out exactly. Defects are injected onto
this clean world afterwards (see defects.py) so ground truth always records the
pre-mutation reality.

All money fields are Python int (paise). Dates are datetime.date / datetime.datetime.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .calendar import days_in_window, next_banking_day, settlement_date
from .fees import compute_expected_fee
from .ids import IdFactory
from .profiles import (
    ADJUSTMENT_MAX_PAISE,
    ADJUSTMENT_PER_1000_SETTLEMENTS,
    CAPTURED_PER_1000,
    CHARGEBACK_PER_1000,
    FAILED_PER_1000,
    OPENING_BALANCE_PAISE,
    PARTIAL_REFUND_SHARE_PER_1000,
    REFUND_RATE_PER_1000,
    allocate_daily_volume,
    pick_amount_paise,
    pick_method,
)


@dataclass
class Order:
    order_id: str
    order_date: date
    customer_id: str
    gross_paise: int
    currency: str
    intended_method: str
    status: str
    invoice_no: str


@dataclass
class Payment:
    payment_id: str
    order_id: str
    captured_at: datetime | None
    method: str
    international: bool
    gross_paise: int
    fee_paise: int
    gst_paise: int
    net_paise: int
    status: str
    settlement_id: str | None
    refund_id: str | None = None
    refund_paise: int = 0


@dataclass
class Settlement:
    settlement_id: str
    settled_at: date
    utr: str
    payment_ids: list[str] = field(default_factory=list)
    num_payments: int = 0
    gross_paise: int = 0
    fee_paise: int = 0
    gst_paise: int = 0
    adjustment_paise: int = 0
    net_paise: int = 0


@dataclass
class BankTxn:
    bank_txn_id: str
    value_date: date
    narration: str
    credit_paise: int
    debit_paise: int
    balance_paise: int
    # Not part of the public CSV schema, but tracked internally / joined out at emit time.
    settlement_id: str | None = None
    kind: str = "settlement_credit"  # settlement_credit | customer_credit | chargeback_debit


@dataclass
class Chargeback:
    bank_txn_id: str
    payment_id: str
    amount_paise: int


@dataclass
class World:
    seed: int
    end_date: date
    months: int
    orders: list[Order] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    bank_txns: list[BankTxn] = field(default_factory=list)
    chargebacks: list[Chargeback] = field(default_factory=list)
    ids: IdFactory | None = None


def _child_rng(root: random.Random) -> random.Random:
    return random.Random(root.getrandbits(64))


def build_clean_world(records: int, seed: int, months: int, end_date: date) -> World:
    root = random.Random(seed)
    ids = IdFactory(_child_rng(root))
    order_rng = _child_rng(root)
    payment_rng = _child_rng(root)
    refund_rng = _child_rng(root)
    settle_rng = _child_rng(root)
    bank_rng = _child_rng(root)

    world = World(seed=seed, end_date=end_date, months=months, ids=ids)

    days = days_in_window(end_date, months)
    daily_counts = allocate_daily_volume(order_rng, days, records)

    # --- Orders + Payments -------------------------------------------------
    sequence = 0
    for day, count in zip(days, daily_counts):
        for _ in range(count):
            sequence += 1
            method, international = pick_method(order_rng)
            gross = pick_amount_paise(order_rng, method, international)
            order_id = ids.order()
            customer_id = ids.customer()
            currency = "USD" if international else "INR"

            outcome_roll = payment_rng.randrange(1000)
            if outcome_roll < CAPTURED_PER_1000:
                payment_status = "captured"
                order_status = "paid"
            elif outcome_roll < CAPTURED_PER_1000 + FAILED_PER_1000:
                payment_status = "failed"
                order_status = "payment_failed"
            else:
                payment_status = "authorized"
                order_status = "pending"

            order = Order(
                order_id=order_id,
                order_date=day,
                customer_id=customer_id,
                gross_paise=gross,
                currency=currency,
                intended_method=method,
                status=order_status,
                invoice_no=ids.invoice_no(sequence),
            )
            world.orders.append(order)

            capture_minute = payment_rng.randrange(9 * 60, 23 * 60)
            captured_at = datetime.combine(day, datetime.min.time()) + timedelta(minutes=capture_minute)
            fee, gst, net = compute_expected_fee(gross, method, international)

            payment = Payment(
                payment_id=ids.payment(),
                order_id=order_id,
                captured_at=captured_at if payment_status != "failed" else None,
                method=method,
                international=international,
                gross_paise=gross,
                fee_paise=fee if payment_status == "captured" else 0,
                gst_paise=gst if payment_status == "captured" else 0,
                net_paise=net if payment_status == "captured" else 0,
                status=payment_status,
                settlement_id=None,
            )
            world.payments.append(payment)

    # --- Refunds on a slice of captured payments ---------------------------
    captured = [p for p in world.payments if p.status == "captured"]
    for payment in captured:
        if refund_rng.randrange(1000) >= REFUND_RATE_PER_1000:
            continue
        if refund_rng.randrange(1000) < PARTIAL_REFUND_SHARE_PER_1000:
            refund_amount = max(100, (payment.gross_paise * refund_rng.randrange(200, 700)) // 1000)
            refund_amount = refund_amount - (refund_amount % 100) or 100
        else:
            refund_amount = payment.net_paise
        payment.refund_id = ids.refund()
        payment.refund_paise = min(refund_amount, payment.net_paise)

    # --- Group captured payments into settlements ---------------------------
    settlement_by_date_method: dict[tuple[date, str], Settlement] = {}
    for payment in captured:
        capture_day = payment.captured_at.date()
        settle_day = settlement_date(capture_day, payment.method)
        key = (settle_day, payment.method)
        settlement = settlement_by_date_method.get(key)
        if settlement is None:
            settlement = Settlement(settlement_id=ids.settlement(), settled_at=settle_day, utr=ids.utr())
            settlement_by_date_method[key] = settlement
            world.settlements.append(settlement)
        settlement.payment_ids.append(payment.payment_id)
        settlement.num_payments += 1
        settlement.gross_paise += payment.gross_paise
        settlement.fee_paise += payment.fee_paise
        settlement.gst_paise += payment.gst_paise
        settlement.net_paise += payment.net_paise - payment.refund_paise
        payment.settlement_id = settlement.settlement_id

    # Attach adjustments and finalize net.
    for settlement in world.settlements:
        if settle_rng.randrange(1000) < ADJUSTMENT_PER_1000_SETTLEMENTS:
            adjustment = settle_rng.randrange(-ADJUSTMENT_MAX_PAISE, ADJUSTMENT_MAX_PAISE + 1)
            adjustment = adjustment - (adjustment % 100)
        else:
            adjustment = 0
        settlement.adjustment_paise = adjustment
        settlement.net_paise += adjustment

    # --- Legitimate chargebacks on a slice of captured payments -------------
    eligible_for_cb = [p for p in captured if p.refund_id is None]
    for payment in eligible_for_cb:
        if settle_rng.randrange(1000) >= CHARGEBACK_PER_1000:
            continue
        world.chargebacks.append(
            Chargeback(bank_txn_id="", payment_id=payment.payment_id, amount_paise=payment.net_paise)
        )

    # --- Bank statement: one credit per settlement + chargeback debits ------
    from .narration import chargeback_narration, settlement_narration

    events: list[tuple[date, str, int, int, str, str | None, int | None]] = []
    # (value_date, sort_tiebreak, credit, debit, narration, settlement_id)
    for settlement in world.settlements:
        value_date = next_banking_day(settlement.settled_at)
        narration = settlement_narration(bank_rng, settlement.utr)
        events.append((value_date, settlement.settlement_id, settlement.net_paise, 0, narration, settlement.settlement_id, None))

    # events: (value_date, sort_tiebreak, credit, debit, narration, settlement_id, chargeback_index)
    for cb_index, cb in enumerate(world.chargebacks):
        payment = next(p for p in world.payments if p.payment_id == cb.payment_id)
        cb_value_date = next_banking_day(payment.captured_at.date() + timedelta(days=bank_rng.randrange(5, 20)))
        ref = ids.bank_txn()
        events.append((cb_value_date, "cb_" + ref, 0, cb.amount_paise, chargeback_narration(bank_rng, ref), None, cb_index))

    events.sort(key=lambda e: (e[0], e[1]))

    balance = OPENING_BALANCE_PAISE
    for value_date, _tiebreak, credit, debit, narration, settlement_id, cb_index in events:
        balance += credit - debit
        txn = BankTxn(
            bank_txn_id=ids.bank_txn(),
            value_date=value_date,
            narration=narration,
            credit_paise=credit,
            debit_paise=debit,
            balance_paise=balance,
            settlement_id=settlement_id,
            kind="settlement_credit" if settlement_id else "chargeback_debit",
        )
        world.bank_txns.append(txn)
        if cb_index is not None:
            world.chargebacks[cb_index].bank_txn_id = txn.bank_txn_id

    return world
