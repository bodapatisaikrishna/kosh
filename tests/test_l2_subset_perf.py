"""Phase 4 checkpoint: p99 solve time < 250ms.

The reference fixture hands L2 zero real residual (see ARCHITECTURE.md), so this
is verified against the algorithm directly over synthetic instances at realistic
settlement scale, not against a dataset that happens not to exercise it. Kept
deterministic (fixed seed) so it's a real regression guard, not a flaky timing test.
"""

from __future__ import annotations

import random
import time

from engine.l2_subset import DEFAULT_MAX_TERMS, Candidate, solve_subset

TRIALS = 400
SEED = 42
P99_BUDGET_SECONDS = 0.25


def _synthetic_instance(rng: random.Random):
    n = rng.randrange(2, DEFAULT_MAX_TERMS + 1)
    amounts = [rng.randrange(500_00, 50_000_00) for _ in range(n)]
    if n >= 3 and rng.random() < 0.3:
        idx = rng.randrange(n)
        amounts[idx] = -abs(amounts[idx]) // 3
    candidates = [Candidate(id=f"c{i}", amount_paise=amt) for i, amt in enumerate(amounts)]
    subset_size = rng.randrange(1, n + 1)
    target = sum(amounts[i] for i in rng.sample(range(n), subset_size))
    if rng.random() < 0.1:
        target += rng.randrange(-500, 500)
    return target, candidates


def test_p99_solve_time_under_250ms():
    rng = random.Random(SEED)
    timings = []
    for _ in range(TRIALS):
        target, candidates = _synthetic_instance(rng)
        started = time.perf_counter()
        solve_subset(target, candidates)
        timings.append(time.perf_counter() - started)

    timings.sort()
    p99 = timings[int(len(timings) * 99 // 100)]
    assert p99 < P99_BUDGET_SECONDS, f"p99={p99*1000:.1f}ms exceeds the {P99_BUDGET_SECONDS*1000:.0f}ms budget"
    assert max(timings) < 0.25 + 0.05, f"max={max(timings)*1000:.1f}ms - even a rare outlier should stay near the cap"
