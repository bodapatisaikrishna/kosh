"""Bank narration synthesis and mangling.

Bank statement narration is free text assembled by whichever core banking system wrote
it. The UTR is buried somewhere inside, the merchant name is inconsistent, and long
narrations get truncated by the statement export. All of that is signal the engine has
to survive, so it is generated faithfully here.
"""

from __future__ import annotations

import random

TEMPLATES: tuple[str, ...] = (
    "NEFT-{utr}-RAZORPAY SOFTWARE PVT LTD",
    "UPI/CR/{utr}/RZPY/SETTLEMENT",
    "IMPS/{utr}/RAZORPAYSOFT/",
    "NEFT CR-HDFC0000060-RAZORPAY SOFTWARE-{utr}",
    "{utr} RZP SETTLE",
)

CUSTOMER_TEMPLATES: tuple[str, ...] = (
    "NEFT-{utr}-{name}",
    "IMPS/{utr}/{name}/PAYMENT",
    "UPI/CR/{utr}/{name}/ORDERPAY",
    "NEFT CR-SBIN0001234-{name}-{utr}",
)

CHARGEBACK_TEMPLATES: tuple[str, ...] = (
    "CHARGEBACK DR-{ref}-RAZORPAY SOFTWARE",
    "DISPUTE DEBIT/{ref}/RZPY",
    "CB RECOVERY {ref} RAZORPAY",
)

CUSTOMER_NAMES: tuple[str, ...] = (
    "R KUMAR", "PRIYA S", "AJAY MEHTA", "S IYER", "NEHA GUPTA",
    "M RAJAN", "FARHAN ALI", "D CHAKRABORTY", "K VENKATESH", "ANITA JOSHI",
)

TRUNCATION_LENGTH = 35


def settlement_narration(rng: random.Random, utr: str) -> str:
    return rng.choice(TEMPLATES).format(utr=utr)


def customer_narration(rng: random.Random, utr: str) -> str:
    return rng.choice(CUSTOMER_TEMPLATES).format(utr=utr, name=rng.choice(CUSTOMER_NAMES))


def chargeback_narration(rng: random.Random, ref: str) -> str:
    return rng.choice(CHARGEBACK_TEMPLATES).format(ref=ref)


def truncate(narration: str) -> str:
    """Statement exports clip long narration at a fixed width."""
    return narration[:TRUNCATION_LENGTH]


def transpose_digits(rng: random.Random, narration: str, utr: str) -> str:
    """Swap two adjacent digits of the UTR inside the narration (OCR / rekeying error)."""
    positions = [i for i, ch in enumerate(utr) if ch.isdigit()]
    if len(positions) < 2:
        return narration
    index = rng.randrange(len(positions) - 1)
    first, second = positions[index], positions[index + 1]
    chars = list(utr)
    chars[first], chars[second] = chars[second], chars[first]
    return narration.replace(utr, "".join(chars))
