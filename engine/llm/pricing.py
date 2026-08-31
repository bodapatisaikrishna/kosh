"""Real $ cost for L3 LLM usage, from published per-token rates.

Phase 5's own checkpoint is "cost per 1000 records < $0.50" - a number that
was silently always 0 until this module existed, because
`EngineMeta.cost_usd_micros` was never populated by anything (see
ARCHITECTURE.md). Token counts (input_tokens/output_tokens) were tracked
correctly all along; only the USD conversion was missing.

Pricing note: NVIDIA does not publish a per-token price for the hosted NIM
preview endpoint (`integrate.api.nvidia.com`) used by `NimClient` - it's a
developer-preview endpoint, not a metered production SKU. The rate below is
the publicly listed price for this model via a third-party inference
marketplace (OpenRouter, https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b,
checked 2026-08-31), used here as the best available honest proxy for "what
this would cost at production API rates" rather than a rate NVIDIA itself
bills. That provenance is stated plainly rather than presented as an official
NIM price list.

Anthropic's official published rates (https://platform.claude.com/docs/en/about-claude/pricing,
checked 2026-08-31) are included for `AnthropicClient`, which is spec-complete
but has never been run live in this project (see README's documented
deviation) - so this project's real, committed cost figure only ever uses
the NIM row below.
"""

from __future__ import annotations

# USD per 1,000,000 tokens: (input_price, output_price).
# A price of $X per 1M tokens equals X micro-dollars per token exactly
# (1 micro-dollar = 1e-6 USD), so cost_usd_micros = tokens * price_per_1m,
# no further scaling needed - see cost_usd_micros() below.
PRICING_USD_PER_1M_TOKENS: dict[str, tuple[float, float]] = {
    # NVIDIA NIM, via NimClient - the model this project actually ran live.
    # Source: OpenRouter listing (third-party marketplace rate, not an
    # NVIDIA-published NIM price - see module note above).
    "nvidia/nemotron-3-ultra-550b-a55b": (0.50, 2.20),
    # Anthropic, via AnthropicClient - never run live in this project.
    # Source: platform.claude.com/docs/en/about-claude/pricing (official).
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def cost_usd_micros(model: str, input_tokens: int, output_tokens: int) -> int:
    """Real $ cost for one call's token usage, in micro-dollars (integer).

    Returns 0 for a model with no entry above - a fake/mock client used in
    tests (model_name="fake") has no real-world cost, and an unrecognised
    real model is reported as 0 rather than a guessed rate, so a missing
    price entry reads as "not costed" rather than silently wrong.
    """
    rates = PRICING_USD_PER_1M_TOKENS.get(model)
    if rates is None:
        return 0
    in_price, out_price = rates
    return round(input_tokens * in_price + output_tokens * out_price)
