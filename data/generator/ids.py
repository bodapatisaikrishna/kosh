"""Deterministic, collision-checked identifier minting.

All IDs come from a seeded random.Random so a run is reproducible. Never uses hash():
PYTHONHASHSEED is randomised per process and would break byte-identical output.
"""

from __future__ import annotations

import random

_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIGITS = "0123456789"
_ALNUM_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Real NEFT/RTGS UTRs are 16 characters: a 4-letter bank code, N or R, then 11 digits.
_UTR_BANKS = ("HDFC", "ICIC", "UTIB", "KKBK", "SBIN")

DISPUTE_REF_PREFIX = "DSP"
DISPUTE_REF_LEN = 11  # "DSP" + 8 chars


def derive_dispute_ref(payment_id: str) -> str:
    """Deterministic, one-way-looking (but fully reversible-by-lookup) transform of
    a payment_id into an 11-char token embeddable in bank narration - the same
    "reference the engine can independently recompute" pattern a settlement's UTR
    already uses, applied to chargebacks instead. Pure function: both the generator
    (to embed it in a legitimate chargeback's narration) and the engine (to look it
    up against every known payment_id) call this exact same code, so there is
    nothing to keep in sync by hand.
    """
    payload = payment_id.removeprefix("pay_").upper()
    return DISPUTE_REF_PREFIX + payload[:DISPUTE_REF_LEN - len(DISPUTE_REF_PREFIX)]


class IdFactory:
    """Mints razorpay-shaped IDs and bank UTRs from one seeded RNG."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._seen: set[str] = set()

    def _token(self, n: int, alphabet: str = _B62) -> str:
        return "".join(self._rng.choice(alphabet) for _ in range(n))

    def _unique(self, prefix: str, n: int, alphabet: str = _B62) -> str:
        while True:
            candidate = prefix + self._token(n, alphabet)
            if candidate not in self._seen:
                self._seen.add(candidate)
                return candidate

    def order(self) -> str:
        return self._unique("order_", 14)

    def payment(self) -> str:
        return self._unique("pay_", 14)

    def refund(self) -> str:
        return self._unique("rfnd_", 14)

    def settlement(self) -> str:
        return self._unique("setl_", 14)

    def bank_txn(self) -> str:
        return self._unique("btxn_", 12)

    def customer(self) -> str:
        return self._unique("cust_", 12)

    def utr(self) -> str:
        while True:
            candidate = self._rng.choice(_UTR_BANKS) + "N" + self._token(11, _DIGITS)
            if candidate not in self._seen:
                self._seen.add(candidate)
                return candidate

    def payout_ref(self) -> str:
        """A consolidated-payout batch reference. Deliberately not UTR-shaped (3
        leading letters, not 4+N), so UTR extraction finds nothing joinable."""
        return "PYT" + self._token(10, _DIGITS)

    def invoice_no(self, sequence: int) -> str:
        return f"INV-2026-{sequence:06d}"

    def orphan_dispute_ref(self, avoid: set[str]) -> str:
        """A dispute-ref-shaped token for an orphan chargeback: same format as a
        real derive_dispute_ref() output, so an orphan chargeback's narration is
        textually indistinguishable in *shape* from a legitimate one - only the
        lookup against real payment_ids fails. `avoid` is the set of every real
        payment's derived code for this run, so this can never accidentally
        resolve to a genuine payment by coincidence.
        """
        while True:
            candidate = DISPUTE_REF_PREFIX + self._token(DISPUTE_REF_LEN - len(DISPUTE_REF_PREFIX), _ALNUM_UPPER)
            if candidate not in avoid:
                return candidate
