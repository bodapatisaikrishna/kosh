"""MDR + GST fee model for an Indian payment gateway.

This module is the single source of truth for fee economics. The generator uses it
to build the dataset and the engine imports it to compute expected values, so a fee
bug surfaces as a test failure instead of a plausible-looking wrong match.

Every amount is integer paise. There are no floats in this file, by design:
percentages are basis points and rounding is exact integer half-up.
"""

from __future__ import annotations

# Merchant discount rate in basis points (1 bp = 0.01%), keyed by (method, international).
# Zero-MDR UPI / RuPay debit is the actual regulatory position for P2M in India, which is
# why most UPI volume reconciles trivially: net == gross. The interesting failures
# concentrate in card and international volume.
MDR_BPS: dict[tuple[str, bool], int] = {
    ("upi", False): 0,
    ("rupay_debit", False): 0,
    ("card", False): 200,
    ("netbanking", False): 190,
    ("wallet", False): 200,
    ("card", True): 300,
}

# GST on the gateway fee: 18%.
GST_BPS = 1800

METHODS: tuple[str, ...] = ("upi", "rupay_debit", "card", "netbanking", "wallet")


class UnknownFeeTier(KeyError):
    """Raised when a (method, international) pair has no configured MDR."""


def round_half_up_div(numerator: int, denominator: int) -> int:
    """Integer division rounding halves away from zero.

    Python's builtin round() is banker's rounding (round-half-to-even), which is wrong
    for money. This is exact: no float ever enters the computation.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator >= 0:
        return (2 * numerator + denominator) // (2 * denominator)
    return -((-2 * numerator + denominator) // (2 * denominator))


def mdr_bps(method: str, international: bool) -> int:
    try:
        return MDR_BPS[(method, international)]
    except KeyError as exc:
        raise UnknownFeeTier(f"no MDR configured for method={method!r} international={international!r}") from exc


def compute_expected_fee(gross_paise: int, method: str, international: bool) -> tuple[int, int, int]:
    """Return (fee_paise, gst_paise, net_paise) for a capture.

    Rounding is applied at each step - fee first, then GST on the rounded fee - not once
    at the end. That ordering is what real gateways do and it produces genuine sub-paisa
    drift when you try to re-derive totals from aggregates. Keep it.
    """
    if gross_paise < 0:
        raise ValueError("gross_paise must be non-negative")
    fee = round_half_up_div(gross_paise * mdr_bps(method, international), 10_000)
    gst = round_half_up_div(fee * GST_BPS, 10_000)
    return fee, gst, gross_paise - fee - gst
