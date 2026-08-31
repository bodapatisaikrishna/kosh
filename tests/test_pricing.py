"""engine/llm/pricing.py: real $ cost from token counts.

This closes the gap where EngineMeta.cost_usd_micros was always 0 - the
Phase 5 checkpoint ("cost per 1000 records < $0.50") had no real number
behind it until this existed. See ARCHITECTURE.md.
"""

from __future__ import annotations

from engine.llm.pricing import cost_usd_micros


def test_known_model_computes_real_cost():
    # $0.50/1M input, $2.20/1M output for nvidia/nemotron-3-ultra-550b-a55b.
    # 105556*0.50 + 10051*2.20 = 52778 + 22112.2 = 74890.2 -> rounds to 74890.
    micros = cost_usd_micros("nvidia/nemotron-3-ultra-550b-a55b", 105556, 10051)
    assert micros == 74890


def test_zero_tokens_costs_zero():
    assert cost_usd_micros("nvidia/nemotron-3-ultra-550b-a55b", 0, 0) == 0


def test_unknown_model_reports_zero_not_a_guess():
    # A fake/mock test client, or a real model with no pricing entry, must
    # never be silently priced at some other model's rate.
    assert cost_usd_micros("fake", 1_000_000, 1_000_000) == 0


def test_result_is_a_real_int_never_a_float():
    micros = cost_usd_micros("nvidia/nemotron-3-ultra-550b-a55b", 1, 1)
    assert isinstance(micros, int)
