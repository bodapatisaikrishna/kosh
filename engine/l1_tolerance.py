"""L1: tolerance matching for whatever L0's exact-key cascade could not confidently
place - mainly bank credits whose UTR was mangled beyond exact/prefix recovery (a
transposed digit lands *inside* the UTR, so no prefix survives intact).

A candidate must satisfy all three: amount within tolerance, date within the
settlement window, and narration that reads like a genuine settlement credit. And
it must be the *only* one that does - a tie is escalated, never picked.
"""

from __future__ import annotations

from datetime import date

from .contract import Match
from .io import BankRow, Dataset, SettlementRow
from .normalize import settlement_narration_similarity

AMOUNT_TOLERANCE_PAISE = 300
DATE_WINDOW_DAYS = 3
NARRATION_SIMILARITY_THRESHOLD = 0.80


def _date_distance_days(a: date, b: date) -> int:
    return abs((a - b).days)


def find_candidates(txn: BankRow, settlements: list[SettlementRow]) -> list[SettlementRow]:
    txn_date = date.fromisoformat(txn.value_date)
    similarity = settlement_narration_similarity(txn.narration)
    if similarity < NARRATION_SIMILARITY_THRESHOLD:
        return []
    candidates = []
    for s in settlements:
        if abs(s.net_paise - txn.credit_paise) > AMOUNT_TOLERANCE_PAISE:
            continue
        if _date_distance_days(date.fromisoformat(s.settled_at), txn_date) > DATE_WINDOW_DAYS:
            continue
        candidates.append(s)
    return candidates


def match_settlement_bank_txn(dataset: Dataset, residual: list[BankRow]) -> list[Match]:
    matches: list[Match] = []
    for txn in residual:
        candidates = find_candidates(txn, dataset.settlements)
        if len(candidates) != 1:
            continue  # 0: no plausible settlement. >=2: ambiguous - never guess.
        settlement = candidates[0]
        matches.append(Match(
            layer="L1",
            link_type="settlement_bank_txn",
            left_id=settlement.settlement_id,
            right_id=txn.bank_txn_id,
            confidence=0.9,
            evidence=(
                f"amount within +/-{AMOUNT_TOLERANCE_PAISE}p "
                f"(settlement {settlement.net_paise}p vs credit {txn.credit_paise}p)",
                f"date within +/-{DATE_WINDOW_DAYS}d "
                f"(settled_at {settlement.settled_at} vs value_date {txn.value_date})",
                f"narration reads as a settlement credit (similarity {settlement_narration_similarity(txn.narration):.2f})",
                "unique candidate among all settlements",
            ),
        ))
    return matches
