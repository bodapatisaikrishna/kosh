"""Merchant profile: method mix, ticket-size distribution, volume seasonality.

Everything here is integer arithmetic. Weights are relative integers and amounts are
drawn from integer paise buckets, so no float ever touches a money value - not even
during distribution sampling.
"""

from __future__ import annotations

import random
from datetime import date

from .calendar import is_bank_holiday

# (method, international, weight-per-1000). Mirrors a mid-size Indian D2C merchant:
# UPI dominant, cards second, a thin but high-value international tail.
METHOD_MIX: tuple[tuple[str, bool, int], ...] = (
    ("upi", False, 550),
    ("card", False, 200),
    ("netbanking", False, 100),
    ("wallet", False, 80),
    ("rupay_debit", False, 50),
    ("card", True, 20),
)

# Ticket-size buckets per method: (weight, min_paise, max_paise).
TICKET_BUCKETS: dict[tuple[str, bool], tuple[tuple[int, int, int], ...]] = {
    ("upi", False): ((45, 9_900, 79_900), (40, 79_900, 299_900), (15, 299_900, 1_499_900)),
    ("rupay_debit", False): ((40, 29_900, 149_900), (45, 149_900, 499_900), (15, 499_900, 1_999_900)),
    ("card", False): ((30, 49_900, 249_900), (45, 249_900, 999_900), (25, 999_900, 6_999_900)),
    ("netbanking", False): ((25, 99_900, 499_900), (45, 499_900, 1_999_900), (30, 1_999_900, 9_999_900)),
    ("wallet", False): ((60, 4_900, 49_900), (35, 49_900, 199_900), (5, 199_900, 599_900)),
    ("card", True): ((35, 399_900, 1_499_900), (45, 1_499_900, 4_999_900), (20, 4_999_900, 19_999_900)),
}

# Share of captures that get refunded, and of those, how many are partial (per 1000).
REFUND_RATE_PER_1000 = 30
PARTIAL_REFUND_SHARE_PER_1000 = 400

# Payment outcome mix per 1000 attempts.
CAPTURED_PER_1000 = 920
FAILED_PER_1000 = 60
# remainder authorized-but-not-captured

# Legitimate chargebacks (linked to a real payment) per 1000 captures.
CHARGEBACK_PER_1000 = 4

# Settlements occasionally carry a small gateway adjustment (recovery, credit note).
ADJUSTMENT_PER_1000_SETTLEMENTS = 120
ADJUSTMENT_MAX_PAISE = 50_000

OPENING_BALANCE_PAISE = 1_250_000_00

# Day-of-week volume multipliers, x100.
WEEKDAY_MULT = (100, 100, 105, 105, 115, 115, 80)  # Mon..Sun
MONTH_END_MULT = 130  # last three days of a month
HOLIDAY_MULT = 90     # bank holiday: banking stops, shopping does not
FESTIVAL_MULT = 165

# A festival sale window inside the generation range.
FESTIVAL_WINDOWS: tuple[tuple[str, str], ...] = (
    ("2026-08-05", "2026-08-09"),
    ("2026-06-12", "2026-06-15"),
)


def day_weight(d: date) -> int:
    weight = WEEKDAY_MULT[d.weekday()]
    for start, end in FESTIVAL_WINDOWS:
        if date.fromisoformat(start) <= d <= date.fromisoformat(end):
            weight = FESTIVAL_MULT
            break
    if d.day >= 28:
        weight = (weight * MONTH_END_MULT) // 100
    if is_bank_holiday(d):
        weight = (weight * HOLIDAY_MULT) // 100
    return max(weight, 1)


def _weighted_choice(rng: random.Random, weights: list[int]) -> int:
    total = sum(weights)
    pick = rng.randrange(total)
    cursor = 0
    for index, weight in enumerate(weights):
        cursor += weight
        if pick < cursor:
            return index
    return len(weights) - 1


def pick_method(rng: random.Random) -> tuple[str, bool]:
    index = _weighted_choice(rng, [w for _, _, w in METHOD_MIX])
    method, international, _ = METHOD_MIX[index]
    return method, international


def pick_amount_paise(rng: random.Random, method: str, international: bool) -> int:
    buckets = TICKET_BUCKETS[(method, international)]
    index = _weighted_choice(rng, [w for w, _, _ in buckets])
    _, low, high = buckets[index]
    amount = rng.randrange(low, high + 1)
    # Most Indian storefront prices are whole rupees; a minority carry paise.
    if rng.randrange(1000) < 850:
        amount = amount - (amount % 100)
    return max(amount, 100)


def allocate_daily_volume(rng: random.Random, days: list[date], total: int) -> list[int]:
    """Split `total` orders across `days` proportional to seasonality weights.

    Largest-remainder apportionment on integers, so the parts always sum to `total`
    exactly and the result is identical on every run.
    """
    weights = [day_weight(d) for d in days]
    weight_total = sum(weights)
    base = [(total * w) // weight_total for w in weights]
    remainders = [(total * w) % weight_total for w in weights]
    shortfall = total - sum(base)
    order = sorted(range(len(days)), key=lambda i: (-remainders[i], i))
    for i in order[:shortfall]:
        base[i] += 1
    # Jitter without changing the total: move a few orders between adjacent days.
    for _ in range(len(days)):
        source = rng.randrange(len(days))
        target = rng.randrange(len(days))
        if source != target and base[source] > 0:
            moved = min(base[source], rng.randrange(1, 4))
            base[source] -= moved
            base[target] += moved
    return base
