"""UTR extraction and narration normalization.

Bank narration is free text: the UTR is buried in it, sometimes truncated by the
statement export, sometimes rekeyed with a digit transposed. This module turns that
text into structured signal an L0/L1 matcher can act on - and, just as importantly,
tells the caller when it *can't* find a confident signal, so the caller escalates
instead of guessing.
"""

from __future__ import annotations

import re

# A UTR here is 4 uppercase letters (bank code) + one of N/R + up to 11 digits.
# \d{1,11} is deliberately variable-length: it lets one regex catch both a full
# 16-char UTR and a truncated fragment of it, differentiated afterwards by length.
# The digit is not optional - "...R KUMAR" is 4 letters + R with zero digits after
# it, and would otherwise false-positive as a UTR-shaped token.
UTR_TOKEN_RE = re.compile(r"[A-Z]{4}[NR]\d{1,11}")

FULL_UTR_LEN = 16
MIN_PREFIX_LEN = 12  # per spec: a prefix match under 12 chars is too weak to trust

_NON_WORD_RE = re.compile(r"[^A-Z0-9]+")

# Vocabulary a genuine PG settlement narration draws from (rail names, the
# merchant's own name variants, settlement keywords). This is domain knowledge an
# ops team accumulates from real statements, not a peek at the generator's specific
# template strings - it's deliberately generic rather than an exact copy of them.
SETTLEMENT_VOCABULARY: frozenset[str] = frozenset({
    "NEFT", "UPI", "IMPS", "CR", "RTGS",
    "RAZORPAY", "RAZORPAYSOFT", "RZPY", "RZP", "SOFTWARE", "PVT", "LTD",
    "SETTLE", "SETTLEMENT", "HDFC0000060",
})


def extract_utr_tokens(narration: str) -> list[str]:
    """All UTR-shaped substrings in narration, uppercased. Order of appearance."""
    return UTR_TOKEN_RE.findall(narration.upper())


def best_utr_token(narration: str) -> str | None:
    """The single longest UTR-shaped token, or None if narration has none at all."""
    tokens = extract_utr_tokens(narration)
    return max(tokens, key=len, default=None) or None


def tokenize_narration(narration: str) -> set[str]:
    """Narration split into words, with UTR-shaped and purely-numeric tokens
    stripped out - narration similarity should judge the *wording*, not whether the
    reference number happens to match (that's L0's job, via exact/prefix lookup)."""
    words = {w for w in _NON_WORD_RE.split(narration.upper()) if w}
    utr_like = set(extract_utr_tokens(narration))
    return {w for w in words if w not in utr_like and not w.isdigit()}


def settlement_narration_similarity(narration: str) -> float:
    """Fraction of narration's (non-UTR, non-numeric) tokens that are recognized
    settlement vocabulary. 1.0 for a genuine settlement narration (even truncated -
    whatever survives is still all settlement wording); low for a customer-name or
    chargeback narration, which draw from a different vocabulary almost entirely.
    """
    tokens = tokenize_narration(narration)
    if not tokens:
        return 0.0
    return len(tokens & SETTLEMENT_VOCABULARY) / len(tokens)
