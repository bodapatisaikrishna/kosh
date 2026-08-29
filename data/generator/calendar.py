"""Indian banking calendar: holidays, settlement cycles, month-end cutoffs.

Settlements do not land on Sundays, 2nd/4th Saturdays, or bank holidays - they roll
forward. This is what creates legitimate multi-day gaps between capture and credit,
and it is why a naive "capture date + 2" matcher produces false matches.
"""

from __future__ import annotations

from datetime import date, timedelta

# National bank holidays covering the generation window (2025-2026).
BANK_HOLIDAYS: frozenset[date] = frozenset(
    date.fromisoformat(d)
    for d in (
        "2025-01-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14",
        "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02",
        "2025-10-20", "2025-10-21", "2025-11-05", "2025-12-25",
        "2026-01-26", "2026-03-03", "2026-03-21", "2026-03-31", "2026-04-01",
        "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-27", "2026-08-15",
        "2026-08-26", "2026-10-02", "2026-11-08", "2026-11-09", "2026-12-25",
    )
)

# Settlement cycle by method: T+N business days. UPI and RuPay settle a day faster.
SETTLEMENT_TPLUS: dict[str, int] = {
    "upi": 1,
    "rupay_debit": 1,
    "card": 2,
    "netbanking": 2,
    "wallet": 2,
}
DEFAULT_TPLUS = 2


def is_second_or_fourth_saturday(d: date) -> bool:
    if d.weekday() != 5:
        return False
    return ((d.day - 1) // 7) + 1 in (2, 4)


def is_bank_holiday(d: date) -> bool:
    return d.weekday() == 6 or is_second_or_fourth_saturday(d) or d in BANK_HOLIDAYS


def next_banking_day(d: date) -> date:
    while is_bank_holiday(d):
        d = d + timedelta(days=1)
    return d


def settlement_date(capture_day: date, method: str) -> date:
    """Capture day + T+N banking days, rolled forward past holidays."""
    remaining = SETTLEMENT_TPLUS.get(method, DEFAULT_TPLUS)
    cursor = capture_day
    while remaining > 0:
        cursor = cursor + timedelta(days=1)
        if not is_bank_holiday(cursor):
            remaining -= 1
    return cursor


def month_end(d: date) -> date:
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def days_in_window(end: date, months: int) -> list[date]:
    """Inclusive list of dates ending at `end`, spanning `months` * 30 days."""
    span = months * 30
    return [end - timedelta(days=offset) for offset in range(span - 1, -1, -1)]
