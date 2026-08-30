"""L0: exact-key joins, in cascade.

    1. orders.order_id       <-> pg_payments.order_id
    2. pg_payments.settlement_id <-> pg_settlements.settlement_id
    3. pg_settlements.utr    <-> UTR extracted from bank_statement.narration
    4. pg_payments.payment_id <-> a dispute reference extracted from a bank
       debit's narration (derive_dispute_ref - see engine/normalize.py)

Every match is exact and carries confidence 1.0 - there is no fuzziness here by
design. The one place ambiguity can arise (a truncated UTR prefix shared by more
than one settlement) is checked explicitly and refused rather than guessed: an
ambiguous L0 candidate is left for L1, never resolved by picking one.
"""

from __future__ import annotations

from .contract import Match
from .io import BankRow, Dataset
from .normalize import (
    FULL_UTR_LEN,
    MIN_PREFIX_LEN,
    best_utr_token,
    derive_dispute_ref,
    extract_dispute_ref,
)


def match_order_payment(dataset: Dataset) -> list[Match]:
    order_ids = {o.order_id for o in dataset.orders}
    return [
        Match(
            layer="L0",
            link_type="order_payment",
            left_id=p.order_id,
            right_id=p.payment_id,
            confidence=1.0,
            evidence=(f"pg_payments.order_id == orders.order_id ({p.order_id})",),
        )
        for p in dataset.payments
        if p.order_id in order_ids
    ]


def match_payment_settlement(dataset: Dataset) -> list[Match]:
    settlement_ids = {s.settlement_id for s in dataset.settlements}
    return [
        Match(
            layer="L0",
            link_type="payment_settlement",
            left_id=p.payment_id,
            right_id=p.settlement_id,
            confidence=1.0,
            evidence=(f"pg_payments.settlement_id == pg_settlements.settlement_id ({p.settlement_id})",),
        )
        for p in dataset.payments
        if p.settlement_id and p.settlement_id in settlement_ids
    ]


def match_settlement_bank_txn(dataset: Dataset) -> tuple[list[Match], list[BankRow]]:
    """Returns (matches, residual). residual is every credit-bearing bank row L0
    could not confidently tie to a settlement - debits (chargebacks) are never
    candidates here, since a settlement link is only ever the credit leg."""
    by_utr = {s.utr: s for s in dataset.settlements}
    matches: list[Match] = []
    residual: list[BankRow] = []

    for txn in dataset.bank:
        if txn.debit_paise > 0:
            continue  # a genuine debit (chargeback) is never a settlement credit.
            # Note: this deliberately does NOT check credit_paise > 0 - a settlement
            # can legitimately net to exactly Rs 0 (e.g. after a period_cutoff shift
            # on a small settlement), producing a credit row with credit_paise == 0
            # that is still a real settlement credit, not a debit.

        token = best_utr_token(txn.narration)
        if token is None:
            residual.append(txn)
            continue

        if len(token) == FULL_UTR_LEN:
            settlement = by_utr.get(token)
            if settlement is not None:
                matches.append(Match(
                    layer="L0",
                    link_type="settlement_bank_txn",
                    left_id=settlement.settlement_id,
                    right_id=txn.bank_txn_id,
                    confidence=1.0,
                    evidence=(f"narration UTR {token} exactly matches pg_settlements.utr",),
                ))
            else:
                # Syntactically a full UTR, but it matches no known settlement - a
                # digit was rekeyed somewhere in it. Guessing which settlement this
                # "meant" would be exactly the false match this layer must never
                # produce, so it is left for L1's amount/date/narration approach.
                residual.append(txn)
            continue

        if len(token) >= MIN_PREFIX_LEN:
            candidates = [s for s in dataset.settlements if s.utr.startswith(token)]
            if len(candidates) == 1:
                matches.append(Match(
                    layer="L0",
                    link_type="settlement_bank_txn",
                    left_id=candidates[0].settlement_id,
                    right_id=txn.bank_txn_id,
                    confidence=1.0,
                    evidence=(f"narration UTR prefix {token} uniquely matches pg_settlements.utr {candidates[0].utr}",),
                ))
            # 0 or >=2 candidates: too weak or genuinely ambiguous - never guess.
            else:
                residual.append(txn)
            continue

        residual.append(txn)

    return matches, residual


def match_chargeback_payment(dataset: Dataset) -> tuple[list[Match], list[BankRow]]:
    """Returns (matches, residual). residual is every debit-bearing bank row that
    doesn't resolve to a known payment - a genuine ORPHAN_CHARGEBACK candidate for
    the exception ledger, not a matching failure to escalate further: there is no
    L1/L2 tolerance concept for a chargeback (no amount/date fuzziness makes an
    unlinkable dispute suddenly linkable), so an unresolved one here is final.
    """
    payment_by_ref = {derive_dispute_ref(p.payment_id): p.payment_id for p in dataset.payments}
    matches: list[Match] = []
    residual: list[BankRow] = []

    for txn in dataset.bank:
        if txn.debit_paise <= 0:
            continue  # only debits can be chargebacks

        ref = extract_dispute_ref(txn.narration)
        payment_id = payment_by_ref.get(ref) if ref else None
        if payment_id is not None:
            matches.append(Match(
                layer="L0",
                link_type="chargeback_payment",
                left_id=payment_id,
                right_id=txn.bank_txn_id,
                confidence=1.0,
                evidence=(f"narration dispute ref {ref} exactly matches derive_dispute_ref(pg_payments.payment_id)",),
            ))
        else:
            residual.append(txn)

    return matches, residual
