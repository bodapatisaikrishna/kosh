"""UTR extraction and narration-similarity scoring - the signal L0/L1 act on."""

from __future__ import annotations

from engine.normalize import best_utr_token, extract_utr_tokens, settlement_narration_similarity


def test_extracts_full_utr_from_each_known_template_shape():
    cases = [
        "NEFT-HDFCN12345678901-RAZORPAY SOFTWARE PVT LTD",
        "UPI/CR/ICICN12345678901/RZPY/SETTLEMENT",
        "IMPS/UTIBN12345678901/RAZORPAYSOFT/",
        "NEFT CR-HDFC0000060-RAZORPAY SOFTWARE-KKBKN12345678901",
        "SBINN12345678901 RZP SETTLE",
    ]
    for narration in cases:
        token = best_utr_token(narration)
        assert token is not None and len(token) == 16, narration


def test_truncated_utr_yields_a_shorter_prefix_token():
    """A narration cut off partway through the UTR (12 of its 16 chars survive)
    must still yield a usable, shorter token - this is what feeds L0's prefix-match
    branch. Note: in the reference fixture's own templates, a truncation either
    leaves the UTR fully intact or removes it entirely (its one long-prefix
    template is itself already longer than the 35-char cutoff) - this partial-
    survival case doesn't occur there, but the extraction logic must still handle
    it for other narration shapes / a different truncation width."""
    truncated = "NEFT-HDFCN1234567-CUT"[: len("NEFT-HDFCN1234567")]
    token = best_utr_token(truncated)
    assert token == "HDFCN1234567"
    assert len(token) == 12


def test_no_utr_shaped_token_returns_none():
    assert best_utr_token("NEFT-R KUMAR-PAYMENT") is None


def test_extract_utr_tokens_finds_every_occurrence():
    narration = "REF HDFCN12345678901 AND ALSO ICICN98765432109"
    tokens = extract_utr_tokens(narration)
    assert "HDFCN12345678901" in tokens
    assert "ICICN98765432109" in tokens


def test_settlement_narration_similarity_high_for_genuine_settlement_text():
    assert settlement_narration_similarity("NEFT-HDFCN12345678901-RAZORPAY SOFTWARE PVT LTD") >= 0.80


def test_settlement_narration_similarity_survives_truncation():
    truncated = "NEFT CR-HDFC0000060-RAZORPAY SOFTWARE-HDFCN1234"[:35]
    assert settlement_narration_similarity(truncated) >= 0.80


def test_settlement_narration_similarity_low_for_customer_credit():
    assert settlement_narration_similarity("NEFT-HDFCN12345678901-R KUMAR") < 0.80


def test_settlement_narration_similarity_low_for_chargeback_text():
    assert settlement_narration_similarity("CHARGEBACK DR-ref12345-RAZORPAY SOFTWARE") < 0.80
