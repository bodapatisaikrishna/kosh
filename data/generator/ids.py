"""Deterministic, collision-checked identifier minting.

All IDs come from a seeded random.Random so a run is reproducible. Never uses hash():
PYTHONHASHSEED is randomised per process and would break byte-identical output.
"""

from __future__ import annotations

import random

_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIGITS = "0123456789"

# Real NEFT/RTGS UTRs are 16 characters: a 4-letter bank code, N or R, then 11 digits.
_UTR_BANKS = ("HDFC", "ICIC", "UTIB", "KKBK", "SBIN")


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

    def invoice_no(self, sequence: int) -> str:
        return f"INV-2026-{sequence:06d}"
