"""The 12 defect injectors.

Injectors mutate a clean World in place and append a Defect record to the ground
truth. A record that already carries a defect is skipped by later injectors so the
ground truth stays unambiguous - two overlapping defects on one record would make it
impossible to say which category the engine "should" have raised.

Injectors run in a fixed order (the order below); that order is part of the seed
contract and byte-identical output depends on it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta

from .fees import GST_BPS, compute_expected_fee, mdr_bps, round_half_up_div
from .ids import derive_dispute_ref
from .narration import customer_narration, transpose_digits, truncate
from .world import BankTxn, World

# Target share of orders affected by each defect type, in parts per 1000 orders.
# Sums to ~100 (10%). Every type is also guaranteed a floor count via `max(1, ...)`
# so small runs still exercise all 12.
DEFECT_RATES_PER_1000: dict[str, int] = {
    # Rates are per 1000 *of that injector's own candidate pool* (e.g. captured
    # payments, settlements, international payments), not per 1000 orders - pools
    # are different sizes, so these are tuned so the totals land at ~10% of orders
    # for the reference run_2000 fixture (see manifest.json after generation).
    "missing_settlement": 11,
    "duplicate_payment": 11,
    "rounding_drift": 14,
    "fee_mismatch_wrong_tier": 11,
    "gst_variance": 20,
    "refund_misallocation": 175,
    "orphan_chargeback": 49,
    "period_cutoff": 49,
    "utr_mangled": 66,
    "fx_variance": 350,
    "unidentified_credit": 49,
    "settlement_split": 48,
    # A payment where BOTH the fee tier and the tax rate are wrong at once. Neither
    # single-cause hypothesis in explain_variance can decompose it (the fee-tier
    # check assumes correct GST, the GST-rate check assumes the correct fee), so it
    # lands as a genuinely UNEXPLAINED residual - real work for L3.
    "compound_fee_tax_error": 8,
    # A daily consolidated payout: one bank credit paying 2-4 settlements at once,
    # carrying only a batch reference and no per-settlement UTR. No exact key exists
    # (L0 cannot join) and no single settlement matches the amount (L1 cannot either)
    # - only subset-sum can explain it. This is what real PGs actually do.
    "consolidated_payout": 130,
}

EXCEPTION_CATEGORY: dict[str, str | None] = {
    "missing_settlement": "MISSING_SETTLEMENT",
    "duplicate_payment": "DUPLICATE_PAYMENT",
    "rounding_drift": None,
    "fee_mismatch_wrong_tier": "FEE_VARIANCE",
    "gst_variance": "TAX_VARIANCE",
    "refund_misallocation": "REFUND_MISALLOCATION",
    "orphan_chargeback": "ORPHAN_CHARGEBACK",
    "period_cutoff": "PERIOD_CUTOFF",
    "utr_mangled": None,
    "fx_variance": "FX_VARIANCE",
    "unidentified_credit": "UNIDENTIFIED_CREDIT",
    "settlement_split": None,
    "compound_fee_tax_error": "UNEXPLAINED_VARIANCE",
    "consolidated_payout": None,
}

RESOLVABLE: dict[str, bool] = {
    "missing_settlement": False,
    "duplicate_payment": False,
    "rounding_drift": True,
    "fee_mismatch_wrong_tier": False,
    "gst_variance": False,
    "refund_misallocation": False,
    "orphan_chargeback": False,
    "period_cutoff": False,
    "utr_mangled": True,
    "fx_variance": False,
    "unidentified_credit": False,
    "settlement_split": True,
    "compound_fee_tax_error": False,
    "consolidated_payout": True,
}


@dataclass
class Defect:
    defect_id: str
    type: str
    affected: dict[str, str]
    expected_exception_category: str | None
    amount_at_risk_paise: int
    resolvable_by_engine: bool


@dataclass
class DefectLog:
    defects: list[Defect] = field(default_factory=list)
    _counter: int = 0

    def add(self, dtype: str, affected: dict[str, str], amount_at_risk_paise: int) -> None:
        self._counter += 1
        self.defects.append(
            Defect(
                defect_id=f"DEF-{self._counter:04d}",
                type=dtype,
                affected=affected,
                expected_exception_category=EXCEPTION_CATEGORY[dtype],
                amount_at_risk_paise=abs(amount_at_risk_paise),
                resolvable_by_engine=RESOLVABLE[dtype],
            )
        )


def _sample_target_count(rng: random.Random, pool_size: int, rate_per_1000: int) -> int:
    target = (pool_size * rate_per_1000) // 1000
    return max(1, target) if pool_size > 0 else 0


def _pick_untouched(rng: random.Random, pool: list, touched: set[str], key, count: int) -> list:
    candidates = [item for item in pool if key(item) not in touched]
    rng.shuffle(candidates)
    chosen = candidates[:count]
    for item in chosen:
        touched.add(key(item))
    return chosen


def _adjust_bank_credit_for_settlement(world: World, settlement_id: str, delta_paise: int) -> None:
    """Keep the bank credit for a settlement in sync when an injector changes
    settlement.net_paise after the clean world already wrote a bank txn for it.

    What actually lands in the bank account is whatever the (possibly broken) PG
    ledger computed, not the theoretically correct amount - so a fee-tier or GST
    defect must show up as a bank credit that matches the wrong net, not one that
    silently disagrees with the very ledger it settled. This must run before any
    injector that mutates settlement.net_paise; it assumes a single bank txn is
    still linked to the settlement (true for every injector that runs before the
    settlement-split injector, which is last).
    """
    if delta_paise == 0:
        return
    linked = [t for t in world.bank_txns if t.settlement_id == settlement_id]
    if len(linked) != 1:
        return  # already split or otherwise irregular; leave it to the split/period injectors
    linked[0].credit_paise += delta_paise
    _resequence_balances(world)


def inject_all(world: World, seed: int, rates_per_1000: dict[str, int] | None = None) -> DefectLog:
    rates = rates_per_1000 or DEFECT_RATES_PER_1000
    rng = random.Random(seed)
    log = DefectLog()
    touched_payments: set[str] = set()
    touched_settlements: set[str] = set()
    touched_bank_txns: set[str] = set()

    _inject_missing_settlement(world, rng, log, rates["missing_settlement"], touched_payments, touched_settlements)
    _inject_duplicate_payment(world, rng, log, rates["duplicate_payment"], touched_payments)
    _inject_rounding_drift(world, rng, log, rates["rounding_drift"], touched_payments)
    _inject_fee_mismatch(world, rng, log, rates["fee_mismatch_wrong_tier"], touched_payments)
    _inject_gst_variance(world, rng, log, rates["gst_variance"], touched_payments)
    _inject_compound_fee_tax_error(world, rng, log, rates["compound_fee_tax_error"], touched_payments)
    _inject_refund_misallocation(world, rng, log, rates["refund_misallocation"], touched_payments)
    _inject_orphan_chargeback(world, rng, log, rates["orphan_chargeback"], touched_bank_txns)
    _inject_period_cutoff(world, rng, log, rates["period_cutoff"], touched_settlements)
    _inject_utr_mangled(world, rng, log, rates["utr_mangled"], touched_bank_txns)
    _inject_fx_variance(world, rng, log, rates["fx_variance"], touched_payments)
    _inject_unidentified_credit(world, rng, log, rates["unidentified_credit"])
    _inject_settlement_split(world, rng, log, rates["settlement_split"], touched_settlements, touched_bank_txns)
    # Runs last: it deletes the individual settlement credits it consolidates, so
    # every injector that adjusts a settlement's own bank credit must already be done.
    _inject_consolidated_payout(world, rng, log, rates["consolidated_payout"], touched_settlements, touched_bank_txns)

    return log


# --- 1. Missing settlement -----------------------------------------------

def _inject_missing_settlement(world, rng, log, rate, touched_payments, touched_settlements):
    captured = [p for p in world.payments if p.status == "captured" and p.settlement_id]
    count = _sample_target_count(rng, len(captured), rate)
    chosen = _pick_untouched(rng, captured, touched_payments, lambda p: p.payment_id, count)
    for payment in chosen:
        settlement_id = payment.settlement_id
        settlement = next((s for s in world.settlements if s.settlement_id == settlement_id), None)
        if settlement is None or len(settlement.payment_ids) < 2:
            continue  # don't hollow out a settlement's only payment; skip to avoid a broken clean world
        removed_net = payment.net_paise - payment.refund_paise
        settlement.payment_ids.remove(payment.payment_id)
        settlement.num_payments -= 1
        settlement.gross_paise -= payment.gross_paise
        settlement.fee_paise -= payment.fee_paise
        settlement.gst_paise -= payment.gst_paise
        settlement.net_paise -= removed_net
        _adjust_bank_credit_for_settlement(world, settlement.settlement_id, -removed_net)
        payment.settlement_id = None
        log.add("missing_settlement", {"payment_id": payment.payment_id}, payment.net_paise)


# --- 2. Duplicate payment --------------------------------------------------

def _inject_duplicate_payment(world, rng, log, rate, touched_payments):
    from .world import Payment

    captured = [p for p in world.payments if p.status == "captured"]
    count = _sample_target_count(rng, len(captured), rate)
    chosen = _pick_untouched(rng, captured, touched_payments, lambda p: p.payment_id, count)
    for payment in chosen:
        dup_id = world.ids.payment()
        duplicate = Payment(
            payment_id=dup_id,
            order_id=payment.order_id,
            captured_at=payment.captured_at + timedelta(minutes=1) if payment.captured_at else None,
            method=payment.method,
            international=payment.international,
            gross_paise=payment.gross_paise,
            fee_paise=payment.fee_paise,
            gst_paise=payment.gst_paise,
            net_paise=payment.net_paise,
            status="captured",
            settlement_id=None,
        )
        world.payments.append(duplicate)
        log.add(
            "duplicate_payment",
            {"payment_id": payment.payment_id, "duplicate_payment_id": dup_id},
            payment.net_paise,
        )


# --- 3. Rounding drift (resolvable) ---------------------------------------

def _inject_rounding_drift(world, rng, log, rate, touched_payments):
    captured = [p for p in world.payments if p.status == "captured" and p.settlement_id]
    count = _sample_target_count(rng, len(captured), rate)
    chosen = _pick_untouched(rng, captured, touched_payments, lambda p: p.payment_id, count)
    for payment in chosen:
        drift = rng.choice([-3, -2, -1, 1, 2, 3])
        settlement = next(s for s in world.settlements if s.settlement_id == payment.settlement_id)
        payment.net_paise += drift
        settlement.net_paise += drift
        _adjust_bank_credit_for_settlement(world, settlement.settlement_id, drift)
        log.add("rounding_drift", {"payment_id": payment.payment_id}, drift)


# --- 4. Wrong MDR tier -----------------------------------------------------

def _inject_fee_mismatch(world, rng, log, rate, touched_payments):
    captured = [p for p in world.payments if p.status == "captured" and p.settlement_id]
    count = _sample_target_count(rng, len(captured), rate)
    chosen = _pick_untouched(rng, captured, touched_payments, lambda p: p.payment_id, count)
    for payment in chosen:
        wrong_method = "card" if payment.method != "card" else "netbanking"
        wrong_intl = payment.international if wrong_method == "card" else False
        try:
            wrong_fee = round_half_up_div(payment.gross_paise * mdr_bps(wrong_method, wrong_intl), 10_000)
        except KeyError:
            continue
        if wrong_fee == payment.fee_paise:
            continue
        wrong_gst = round_half_up_div(wrong_fee * GST_BPS, 10_000)
        delta_fee = wrong_fee - payment.fee_paise
        delta_gst = wrong_gst - payment.gst_paise
        settlement = next(s for s in world.settlements if s.settlement_id == payment.settlement_id)
        settlement.fee_paise += delta_fee
        settlement.gst_paise += delta_gst
        settlement.net_paise -= delta_fee + delta_gst
        _adjust_bank_credit_for_settlement(world, settlement.settlement_id, -(delta_fee + delta_gst))
        payment.net_paise -= delta_fee + delta_gst
        payment.fee_paise = wrong_fee
        payment.gst_paise = wrong_gst
        log.add("fee_mismatch_wrong_tier", {"payment_id": payment.payment_id, "settlement_id": settlement.settlement_id}, delta_fee + delta_gst)


# --- 5. GST variance --------------------------------------------------------

def _inject_gst_variance(world, rng, log, rate, touched_payments):
    captured = [p for p in world.payments if p.status == "captured" and p.settlement_id and p.fee_paise > 0]
    count = _sample_target_count(rng, len(captured), rate)
    chosen = _pick_untouched(rng, captured, touched_payments, lambda p: p.payment_id, count)
    for payment in chosen:
        correct_gst = payment.gst_paise
        bad_bps = rng.choice([1200, 1500, 2800])
        wrong_gst = round_half_up_div(payment.fee_paise * bad_bps, 10_000)
        if wrong_gst == correct_gst:
            continue
        delta = wrong_gst - correct_gst
        settlement = next(s for s in world.settlements if s.settlement_id == payment.settlement_id)
        settlement.gst_paise += delta
        settlement.net_paise -= delta
        _adjust_bank_credit_for_settlement(world, settlement.settlement_id, -delta)
        payment.net_paise -= delta
        payment.gst_paise = wrong_gst
        log.add("gst_variance", {"payment_id": payment.payment_id, "settlement_id": settlement.settlement_id}, delta)


# --- 6. Refund misallocation -------------------------------------------------

def _inject_refund_misallocation(world, rng, log, rate, touched_payments):
    refunded = [p for p in world.payments if p.refund_id and p.settlement_id]
    count = _sample_target_count(rng, len(refunded), rate)
    chosen = _pick_untouched(rng, refunded, touched_payments, lambda p: p.payment_id, count)
    for payment in chosen:
        others = [o for o in world.orders if o.order_id != payment.order_id]
        if not others:
            continue
        wrong_order = rng.choice(others)
        correct_order_id = payment.order_id
        log.add(
            "refund_misallocation",
            {"payment_id": payment.payment_id, "correct_order_id": correct_order_id, "booked_order_id": wrong_order.order_id},
            payment.refund_paise,
        )
        # The refund_id stays attached to the payment (that's how the PG ledger reads it),
        # but the order-level booking now points at the wrong order - this is the field
        # a reconciler would trust, and it's wrong.
        payment.order_id = wrong_order.order_id


# --- 7. Orphan chargeback ---------------------------------------------------

def _inject_orphan_chargeback(world, rng, log, rate, touched_bank_txns):
    from .narration import chargeback_narration

    # A same-shaped but non-derivable dispute reference - this is what makes an
    # orphan chargeback textually indistinguishable from a legitimate one *in
    # format*; only the lookup against a real payment_id fails. See ids.py.
    real_dispute_refs = {derive_dispute_ref(p.payment_id) for p in world.payments}

    count = _sample_target_count(rng, max(len(world.settlements), 1), rate)
    for _ in range(count):
        if not world.bank_txns:
            break
        anchor = rng.choice(world.bank_txns)
        amount = rng.randrange(50_00, 500_00)
        amount = amount - (amount % 100)
        orphan_ref = world.ids.orphan_dispute_ref(real_dispute_refs)
        txn = BankTxn(
            bank_txn_id=world.ids.bank_txn(),
            value_date=anchor.value_date,
            narration=chargeback_narration(rng, orphan_ref),
            credit_paise=0,
            debit_paise=amount,
            balance_paise=anchor.balance_paise - amount,
            settlement_id=None,
            kind="chargeback_debit",
        )
        world.bank_txns.append(txn)
        touched_bank_txns.add(txn.bank_txn_id)
        log.add("orphan_chargeback", {"bank_txn_id": txn.bank_txn_id}, amount)
    _resequence_balances(world)


# --- 8. Period cutoff --------------------------------------------------------

def _inject_period_cutoff(world, rng, log, rate, touched_settlements):
    from .calendar import month_end, next_banking_day

    candidates = [s for s in world.settlements if s.settlement_id not in touched_settlements]
    count = _sample_target_count(rng, len(candidates), rate)
    chosen = _pick_untouched(rng, candidates, touched_settlements, lambda s: s.settlement_id, count)
    for settlement in chosen:
        cutoff = month_end(settlement.settled_at)
        pushed = next_banking_day(cutoff + timedelta(days=rng.randrange(1, 4)))
        old_date = settlement.settled_at
        settlement.settled_at = pushed
        for txn in world.bank_txns:
            if txn.settlement_id == settlement.settlement_id:
                txn.value_date = next_banking_day(pushed)
        log.add(
            "period_cutoff",
            {"settlement_id": settlement.settlement_id},
            settlement.net_paise,
        )
        _ = old_date
    _resequence_bank_order(world)
    _resequence_balances(world)


# --- 9. UTR mangled in narration (resolvable via L2) ------------------------

def _inject_utr_mangled(world, rng, log, rate, touched_bank_txns):
    settlement_txns = [t for t in world.bank_txns if t.settlement_id and t.bank_txn_id not in touched_bank_txns]
    count = _sample_target_count(rng, len(settlement_txns), rate)
    chosen = _pick_untouched(rng, settlement_txns, touched_bank_txns, lambda t: t.bank_txn_id, count)
    for txn in chosen:
        settlement = next(s for s in world.settlements if s.settlement_id == txn.settlement_id)
        if rng.random() < 0.5:
            txn.narration = truncate(txn.narration)
        else:
            txn.narration = transpose_digits(rng, txn.narration, settlement.utr)
        log.add("utr_mangled", {"bank_txn_id": txn.bank_txn_id, "settlement_id": settlement.settlement_id}, settlement.net_paise)


# --- 10. FX variance ---------------------------------------------------------

def _inject_fx_variance(world, rng, log, rate, touched_payments):
    intl = [p for p in world.payments if p.international and p.status == "captured" and p.settlement_id]
    count = _sample_target_count(rng, len(intl), rate)
    chosen = _pick_untouched(rng, intl, touched_payments, lambda p: p.payment_id, count)
    orders_by_id = {o.order_id: o for o in world.orders}
    for payment in chosen:
        order = orders_by_id[payment.order_id]
        # FX booked at capture drifted from the order's invoiced rate by a few basis points.
        drift_bps = rng.choice([-250, -150, 150, 250, 400])
        delta = round_half_up_div(order.gross_paise * drift_bps, 10_000)
        payment.gross_paise = order.gross_paise + delta
        fee, gst, net = compute_expected_fee(payment.gross_paise, payment.method, payment.international)
        old_net = payment.net_paise
        payment.fee_paise, payment.gst_paise, payment.net_paise = fee, gst, net
        settlement = next(s for s in world.settlements if s.settlement_id == payment.settlement_id)
        settlement_net_delta = payment.net_paise - old_net
        settlement.gross_paise += payment.gross_paise - order.gross_paise
        settlement.fee_paise += fee - (order.gross_paise * mdr_bps(payment.method, payment.international)) // 10_000
        settlement.net_paise += settlement_net_delta
        _adjust_bank_credit_for_settlement(world, settlement.settlement_id, settlement_net_delta)
        log.add("fx_variance", {"payment_id": payment.payment_id, "order_id": order.order_id}, delta)


# --- 11. Unidentified credit -------------------------------------------------

def _inject_unidentified_credit(world, rng, log, rate):
    count = _sample_target_count(rng, max(len(world.settlements), 1), rate)
    for _ in range(count):
        if not world.bank_txns:
            break
        anchor = rng.choice(world.bank_txns)
        amount = rng.randrange(500_00, 20_000_00)
        amount = amount - (amount % 100)
        utr = world.ids.utr()
        txn = BankTxn(
            bank_txn_id=world.ids.bank_txn(),
            value_date=anchor.value_date,
            narration=customer_narration(rng, utr),
            credit_paise=amount,
            debit_paise=0,
            balance_paise=anchor.balance_paise + amount,
            settlement_id=None,
            kind="customer_credit",
        )
        world.bank_txns.append(txn)
        log.add("unidentified_credit", {"bank_txn_id": txn.bank_txn_id}, amount)
    _resequence_balances(world)


# --- 12. Settlement split across two bank credits (resolvable via L2) -------

def _inject_settlement_split(world, rng, log, rate, touched_settlements, touched_bank_txns):
    from .narration import settlement_narration

    candidates = [
        s for s in world.settlements
        if s.settlement_id not in touched_settlements and s.net_paise > 200 and s.num_payments >= 2
    ]
    count = _sample_target_count(rng, len(candidates), rate)
    chosen = _pick_untouched(rng, candidates, touched_settlements, lambda s: s.settlement_id, count)
    for settlement in chosen:
        txn = next((t for t in world.bank_txns if t.settlement_id == settlement.settlement_id), None)
        if txn is None or txn.bank_txn_id in touched_bank_txns:
            continue
        split_point = settlement.net_paise // 2
        part_a = split_point - (split_point % 100) or 100
        part_b = settlement.net_paise - part_a
        if part_a <= 0 or part_b <= 0:
            continue
        txn.credit_paise = part_a
        second = BankTxn(
            bank_txn_id=world.ids.bank_txn(),
            value_date=txn.value_date,
            narration=settlement_narration(rng, settlement.utr),
            credit_paise=part_b,
            debit_paise=0,
            balance_paise=0,  # recomputed below
            settlement_id=settlement.settlement_id,
            kind="settlement_credit",
        )
        world.bank_txns.append(second)
        touched_bank_txns.add(txn.bank_txn_id)
        touched_bank_txns.add(second.bank_txn_id)
        log.add(
            "settlement_split",
            {"settlement_id": settlement.settlement_id, "bank_txn_id_a": txn.bank_txn_id, "bank_txn_id_b": second.bank_txn_id},
            settlement.net_paise,
        )
    _resequence_bank_order(world)
    _resequence_balances(world)


# --- 13. Compound fee+tax error (genuinely unexplainable -> L3's residual) -----

# Deliberately disjoint from engine/fees.py::KNOWN_WRONG_GST_BPS (1200/1500/2800):
# if the injected rate were one explain_variance already hypothesises about, the
# GST_RATE branch could still decompose this and it would never reach L3.
_COMPOUND_GST_BPS = (1150, 1375, 2550)


def _inject_compound_fee_tax_error(world, rng, log, rate, touched_payments):
    """Wrong MDR tier AND a non-standard tax rate applied together. Each single-cause
    hypothesis in explain_variance assumes the other component is correct, so neither
    fits - the delta is genuinely UNEXPLAINED, which is exactly the residual L3 exists
    to investigate rather than something a rule should silently guess at."""
    captured = [p for p in world.payments if p.status == "captured" and p.settlement_id and p.fee_paise > 0]
    count = _sample_target_count(rng, len(captured), rate)
    chosen = _pick_untouched(rng, captured, touched_payments, lambda p: p.payment_id, count)
    for payment in chosen:
        wrong_method = "card" if payment.method != "card" else "netbanking"
        try:
            wrong_fee = round_half_up_div(payment.gross_paise * mdr_bps(wrong_method, False), 10_000)
        except KeyError:
            continue
        wrong_gst = round_half_up_div(wrong_fee * rng.choice(_COMPOUND_GST_BPS), 10_000)
        new_net = payment.gross_paise - wrong_fee - wrong_gst
        delta = new_net - payment.net_paise
        if delta == 0:
            continue
        settlement = next(s for s in world.settlements if s.settlement_id == payment.settlement_id)
        settlement.fee_paise += wrong_fee - payment.fee_paise
        settlement.gst_paise += wrong_gst - payment.gst_paise
        settlement.net_paise += delta
        _adjust_bank_credit_for_settlement(world, settlement.settlement_id, delta)
        payment.fee_paise, payment.gst_paise, payment.net_paise = wrong_fee, wrong_gst, new_net
        log.add(
            "compound_fee_tax_error",
            {"payment_id": payment.payment_id, "settlement_id": settlement.settlement_id},
            delta,
        )


# --- 14. Consolidated payout (many settlements -> one credit, resolvable via L2) ---

def _inject_consolidated_payout(world, rng, log, rate, touched_settlements, touched_bank_txns):
    """One bank credit pays several same-day settlements at once, carrying only a
    batch reference. L0 finds no UTR to join on; L1 finds no single settlement whose
    amount matches a 2-4 way sum. Only subset-sum can explain it - this is the case
    L2 exists for, and it is what real gateways actually do on a daily payout run."""
    from .narration import consolidated_narration

    by_date: dict = {}
    for t in world.bank_txns:
        if t.kind != "settlement_credit" or not t.settlement_id:
            continue
        if t.bank_txn_id in touched_bank_txns or t.settlement_id in touched_settlements:
            continue
        by_date.setdefault(t.value_date, []).append(t)

    groups = [(d, txns) for d, txns in sorted(by_date.items()) if len(txns) >= 2]
    count = _sample_target_count(rng, len(groups), rate)
    rng.shuffle(groups)

    for value_date, txns in groups[:count]:
        members = txns[: min(len(txns), rng.randrange(2, 5))]
        total = sum(t.credit_paise for t in members)
        if len(members) < 2 or total <= 0:
            continue
        settlement_ids = sorted(t.settlement_id for t in members)
        for t in members:
            world.bank_txns.remove(t)
            touched_bank_txns.add(t.bank_txn_id)
        txn = BankTxn(
            bank_txn_id=world.ids.bank_txn(),
            value_date=value_date,
            narration=consolidated_narration(rng, world.ids.payout_ref()),
            credit_paise=total,
            debit_paise=0,
            balance_paise=0,  # recomputed below
            settlement_id=None,
            settlement_ids=settlement_ids,
            kind="consolidated_credit",
        )
        world.bank_txns.append(txn)
        touched_bank_txns.add(txn.bank_txn_id)
        touched_settlements.update(settlement_ids)
        log.add(
            "consolidated_payout",
            {"bank_txn_id": txn.bank_txn_id, "settlement_ids": ",".join(settlement_ids)},
            total,
        )
    _resequence_bank_order(world)
    _resequence_balances(world)


# --- shared helpers ----------------------------------------------------------

def _resequence_bank_order(world: World) -> None:
    world.bank_txns.sort(key=lambda t: (t.value_date, t.bank_txn_id))


def _resequence_balances(world: World) -> None:
    from .profiles import OPENING_BALANCE_PAISE

    _resequence_bank_order(world)
    balance = OPENING_BALANCE_PAISE
    for txn in world.bank_txns:
        balance += txn.credit_paise - txn.debit_paise
        txn.balance_paise = balance
