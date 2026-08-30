"""Re-exports the generator's fee model so the engine and the generator compute
expected fees from exactly one function - a fee bug shows up as a test failure
(the generator's own invariant tests), never as a plausible-looking wrong match.

explain_variance is engine-only: it's the reconciler's job of turning "these numbers
differ" into "these numbers differ because", not the generator's.
"""

from __future__ import annotations

from dataclasses import dataclass

from data.generator.fees import (
    GST_BPS,
    MDR_BPS,
    METHODS,
    UnknownFeeTier,
    compute_expected_fee,
    mdr_bps,
    round_half_up_div,
)

__all__ = [
    "GST_BPS", "MDR_BPS", "METHODS", "UnknownFeeTier",
    "compute_expected_fee", "mdr_bps", "round_half_up_div",
    "Explanation", "explain_variance",
]

# Rounding drift (defect #3) is injected as a ±1-3 paise nudge - anything within this
# band is treated as ordinary rounding noise, not a real variance.
ROUNDING_TOLERANCE_PAISE = 3

# GST rates injectors substitute in place of the correct 1800 bps (defect #5).
KNOWN_WRONG_GST_BPS = (1200, 1500, 1800, 2800)


@dataclass(frozen=True)
class Explanation:
    """The decomposition of an observed-vs-expected paise delta.

    cause is one of: MATCH, ROUNDING, FEE_TIER, GST_RATE, REFUND, UNEXPLAINED.
    UNEXPLAINED is an honest "I don't know" - it is what feeds the exception ledger
    in Phase 5, not a failure of this function.
    """

    cause: str
    delta_paise: int
    detail: str


def explain_variance(
    observed_paise: int,
    expected_paise: int,
    *,
    gross_paise: int | None = None,
    method: str | None = None,
    international: bool = False,
    known_refund_paise: int | None = None,
) -> Explanation:
    delta = observed_paise - expected_paise
    if delta == 0:
        return Explanation("MATCH", 0, "observed equals expected exactly")

    if abs(delta) <= ROUNDING_TOLERANCE_PAISE:
        return Explanation("ROUNDING", delta, f"{delta} paise is within the +/-{ROUNDING_TOLERANCE_PAISE}p rounding tolerance")

    if known_refund_paise is not None and abs(delta) == known_refund_paise:
        return Explanation("REFUND", delta, f"delta of {delta}p exactly matches the known refund of {known_refund_paise}p")

    if gross_paise is not None and method is not None:
        for candidate_method in METHODS:
            for candidate_intl in (False, True):
                if (candidate_method, candidate_intl) == (method, international):
                    continue
                try:
                    _, _, candidate_net = compute_expected_fee(gross_paise, candidate_method, candidate_intl)
                except UnknownFeeTier:
                    continue
                if candidate_net == observed_paise:
                    return Explanation(
                        "FEE_TIER", delta,
                        f"observed net matches the {candidate_method}/international={candidate_intl} MDR tier, "
                        f"not the booked {method}/international={international} tier",
                    )

        try:
            fee, _, _ = compute_expected_fee(gross_paise, method, international)
        except UnknownFeeTier:
            fee = None
        if fee is not None:
            for wrong_bps in KNOWN_WRONG_GST_BPS:
                if wrong_bps == GST_BPS:
                    continue
                wrong_gst = round_half_up_div(fee * wrong_bps, 10_000)
                if gross_paise - fee - wrong_gst == observed_paise:
                    return Explanation(
                        "GST_RATE", delta,
                        f"observed net matches GST charged at {wrong_bps} bps instead of the correct {GST_BPS} bps",
                    )

    return Explanation("UNEXPLAINED", delta, f"{delta} paise not explained by fee tier, GST rate, a known refund, or rounding")
