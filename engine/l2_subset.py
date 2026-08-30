"""L2: subset-sum solver for a bank credit that batches multiple settlements (or,
more generally, multiple signed money-bearing candidates) with no single exact key
or tolerance match to explain it.

The spec's suggested approaches both have a scaling trap in this domain. Meet-in-
the-middle costs O(2^(n/2)) in the *candidate count* - dangerous at n=40. A bitset
DP (one Python bigint, one bit per achievable paise value) costs O(bits) per
operation - and while that sounds candidate-count-independent, a per-node use of it
during subset *recovery* actually scales with the *money magnitude*: shifting a
many-megabit integer costs O(its bit length) even when the shift amount is small,
because the result is still nearly as large as the input. For real settlement
amounts (lakhs of rupees, i.e. tens of millions of paise) that is not a rounding
error - it single-handedly blew the 250ms budget during profiling.

So this solver does the two jobs with two different tools. A depth-first search
recovers the actual subset(s), pruned with a cheap O(1) sum-bound at every node
(the most any remaining branch could still add) - weaker pruning than an exact
bitset, but its cost depends only on n (<=40), never on the paise magnitude, so a
node-count-based time check gives a real, predictable worst case. No bitset is
built at all: at this candidate count the DFS with sum-bound pruning is already
fast in practice, and skipping the bitset removes the one unbounded step instead of
trying to make it cheap.

Negative amounts (a refund or chargeback netted into a settlement) are handled by a
standard transformation: shift the target by the sum of all negative amounts, then
treat "including" a negative-amount candidate as adding its absolute value back -
this turns the whole problem into ordinary non-negative subset-sum.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

MAX_SOLUTIONS_TO_DETECT = 2  # only need to know "one" vs "more than one" exists
DEFAULT_TOLERANCE_PAISE = 100
DEFAULT_MAX_TERMS = 40
HARD_DEADLINE_SECONDS = 0.25  # the spec's cap: past this, escalate to L3
# The internal budget is set below the hard deadline, and checked frequently, so
# that (budget + worst-case work done between two checks) still lands under the
# hard deadline rather than merely under the budget itself.
DEFAULT_TIME_BUDGET_SECONDS = 0.15
NODE_CHECK_INTERVAL = 200  # how often the DFS checks the wall clock


@dataclass(frozen=True)
class Candidate:
    id: str
    amount_paise: int  # may be negative: a refund/chargeback netted into the batch


@dataclass(frozen=True)
class SubsetSolution:
    status: str  # "SOLVED" | "AMBIGUOUS" | "NONE" | "TIMEOUT"
    chosen_ids: tuple[str, ...] = ()
    achieved_paise: int = 0
    alternative_solutions: tuple[tuple[str, ...], ...] = ()  # populated when AMBIGUOUS


class TooManyCandidates(ValueError):
    pass


def solve_subset(
    target_paise: int,
    candidates: list[Candidate],
    tolerance_paise: int = DEFAULT_TOLERANCE_PAISE,
    max_terms: int = DEFAULT_MAX_TERMS,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> SubsetSolution:
    if len(candidates) > max_terms:
        raise TooManyCandidates(f"{len(candidates)} candidates exceeds max_terms={max_terms}")
    if not candidates:
        return SubsetSolution(status="NONE")

    # Non-negative transform: base = sum of negative amounts (<=0); "including" a
    # negative candidate now contributes its absolute value relative to base.
    base = sum(c.amount_paise for c in candidates if c.amount_paise < 0)
    ids = [c.id for c in candidates]
    weights = [c.amount_paise if c.amount_paise >= 0 else -c.amount_paise for c in candidates]
    adjusted_target = target_paise - base

    total_weight = sum(weights)
    lo = adjusted_target - tolerance_paise
    hi = adjusted_target + tolerance_paise
    if hi < 0 or lo > total_weight:
        return SubsetSolution(status="NONE")
    lo = max(lo, 0)
    hi = min(hi, total_weight)

    # suffix_sum[i] = sum(weights[i:]) - the most any remaining branch could add.
    # This is the only "reachability" pruning used; see the module docstring for
    # why an exact bitset is deliberately not used here, even for this one check.
    suffix_sum = [0] * (len(weights) + 1)
    for i in range(len(weights) - 1, -1, -1):
        suffix_sum[i] = suffix_sum[i + 1] + weights[i]

    deadline = time.perf_counter() + time_budget_seconds
    solutions: list[tuple[str, ...]] = []
    node_count = 0
    timed_out = False

    def dfs(i: int, remaining_lo: int, remaining_hi: int, chosen: list[str]) -> None:
        nonlocal node_count, timed_out
        if timed_out or len(solutions) >= MAX_SOLUTIONS_TO_DETECT:
            return
        node_count += 1
        if node_count % NODE_CHECK_INTERVAL == 0 and time.perf_counter() > deadline:
            timed_out = True
            return

        if i == len(weights):
            # Leaf: every candidate has been decided. Record exactly once here -
            # not at every intermediate node where the window happens to already
            # include zero, which would record the same full assignment multiple
            # times (once per still-undecided suffix) and starve MAX_SOLUTIONS_TO_
            # DETECT of ever reaching a genuinely different assignment.
            if remaining_lo <= 0 <= remaining_hi:
                solutions.append(tuple(chosen))
            return

        # Bound prune: even taking everything left can't reach the window's low
        # end, or we've already overshot its high end.
        if remaining_hi < 0 or remaining_lo > suffix_sum[i]:
            return

        w = weights[i]
        # Exclude weights[i]
        dfs(i + 1, remaining_lo, remaining_hi, chosen)
        if timed_out or len(solutions) >= MAX_SOLUTIONS_TO_DETECT:
            return
        # Include weights[i]
        chosen.append(ids[i])
        dfs(i + 1, remaining_lo - w, remaining_hi - w, chosen)
        chosen.pop()

    # remaining_lo/remaining_hi track the still-needed window relative to the
    # running sum: they start at [lo, hi] and shrink by each included weight.
    dfs(0, lo, hi, [])

    if timed_out and not solutions:
        return SubsetSolution(status="TIMEOUT")

    if not solutions:
        return SubsetSolution(status="NONE")

    def to_true_chosen_ids(transformed_chosen: tuple[str, ...]) -> tuple[str, ...]:
        """Undo the negative-amount transform. In transformed weight-space,
        "chosen" for a non-negative candidate means "included in the batch" -
        but for a negative-amount candidate (a refund/chargeback, folded into
        `base` by default) it means the OPPOSITE: "cancel this negative
        contribution back out", i.e. EXCLUDE it from the batch. So a negative
        candidate is truly included exactly when it was NOT chosen here."""
        transformed_set = set(transformed_chosen)
        return tuple(
            c.id for c in candidates
            if (c.amount_paise >= 0 and c.id in transformed_set)
            or (c.amount_paise < 0 and c.id not in transformed_set)
        )

    # De-duplicate by the true id-set actually chosen (order-independent).
    seen: set[frozenset[str]] = set()
    unique: list[tuple[str, ...]] = []
    for transformed_chosen in solutions:
        true_ids = to_true_chosen_ids(transformed_chosen)
        key = frozenset(true_ids)
        if key not in seen:
            seen.add(key)
            unique.append(true_ids)

    if len(unique) == 1:
        chosen_ids = unique[0]
        achieved = sum(c.amount_paise for c in candidates if c.id in chosen_ids)
        return SubsetSolution(status="SOLVED", chosen_ids=chosen_ids, achieved_paise=achieved)

    return SubsetSolution(status="AMBIGUOUS", alternative_solutions=tuple(unique))


def _profile(trials: int, seed: int, out_path: str | None) -> None:
    """`python -m engine.l2_subset --profile [--trials N] [--seed S] [--out path.json]`

    Runs solve_subset over `trials` synthetic instances at realistic settlement
    scale (pool sizes 2..40, amounts ~Rs 500-Rs 50,000 per candidate) and reports
    the solve-time distribution. Exists because the reference fixture currently
    hands L2 zero real residual - see ARCHITECTURE.md - so the "p99 < 250ms"
    checkpoint is verified against the algorithm directly, not against a dataset
    that happens not to exercise it.
    """
    import json
    import random as _random
    import time as _time

    rng = _random.Random(seed)
    timings_ms: list[float] = []
    status_counts: dict[str, int] = {}

    for _ in range(trials):
        n = rng.randrange(2, DEFAULT_MAX_TERMS + 1)
        amounts = [rng.randrange(500_00, 50_000_00) for _ in range(n)]
        # Occasionally fold in a negative (refund/chargeback) candidate, realistic
        # to the domain this solver actually serves.
        if n >= 3 and rng.random() < 0.3:
            idx = rng.randrange(n)
            amounts[idx] = -abs(amounts[idx]) // 3

        candidates = [Candidate(id=f"c{i}", amount_paise=amt) for i, amt in enumerate(amounts)]
        subset_size = rng.randrange(1, n + 1)
        target = sum(amounts[i] for i in rng.sample(range(n), subset_size))
        if rng.random() < 0.1:
            target += rng.randrange(-500, 500)  # a target with no exact solution

        started = _time.perf_counter()
        result = solve_subset(target, candidates)
        elapsed_ms = (_time.perf_counter() - started) * 1000
        timings_ms.append(elapsed_ms)
        status_counts[result.status] = status_counts.get(result.status, 0) + 1

    timings_ms.sort()
    p50 = timings_ms[len(timings_ms) // 2]
    p99 = timings_ms[int(len(timings_ms) * 99 // 100)]
    report = {
        "trials": trials,
        "seed": seed,
        "max_terms": DEFAULT_MAX_TERMS,
        "p50_ms": p50,
        "p99_ms": p99,
        "max_ms": timings_ms[-1],
        "status_counts": status_counts,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Profile the L2 subset-sum solver against synthetic instances.")
    parser.add_argument("--profile", action="store_true", help="run the timing profile (the only supported mode)")
    parser.add_argument("--trials", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args()
    if args.profile:
        _profile(args.trials, args.seed, args.out)
    else:
        parser.print_help()


DATE_WINDOW_DAYS = 3  # settlements land same-day or T+1 relative to a credit


def match_settlement_bank_txn(dataset, residual, already_matched_settlement_ids: set[str] | None = None):
    """Attempts to explain each still-unresolved bank credit in `residual` as a
    batch of multiple settlements (the "one credit is a batch" case the brief
    describes) - candidates come from settlements within a same-day/T+1 date
    window of the credit, excluding any settlement L0/L1 already confidently
    placed elsewhere. AMBIGUOUS, NONE, TIMEOUT, and "too many candidates" all mean
    "don't guess here" - the credit is simply left unmatched for L3/L4.
    """
    from datetime import date

    from .contract import Match

    already_matched = already_matched_settlement_ids or set()
    matches = []
    for txn in residual:
        txn_date = date.fromisoformat(txn.value_date)
        pool = [
            s for s in dataset.settlements
            if s.settlement_id not in already_matched
            and 0 <= (txn_date - date.fromisoformat(s.settled_at)).days <= DATE_WINDOW_DAYS
        ]
        candidates = [Candidate(id=s.settlement_id, amount_paise=s.net_paise) for s in pool]
        try:
            result = solve_subset(txn.credit_paise, candidates, tolerance_paise=DEFAULT_TOLERANCE_PAISE)
        except TooManyCandidates:
            continue  # too large a pool to solve safely - leave it, don't guess
        if result.status != "SOLVED" or len(result.chosen_ids) < 2:
            continue  # a single-candidate "solve" here would just be an L0/L1 case
            # that already had its chance; only a genuine multi-settlement batch is
            # this layer's job.
        for settlement_id in result.chosen_ids:
            matches.append(Match(
                layer="L2",
                link_type="settlement_bank_txn",
                left_id=settlement_id,
                right_id=txn.bank_txn_id,
                confidence=0.85,
                evidence=(
                    f"subset-sum: {len(result.chosen_ids)} settlements sum to {result.achieved_paise}p, "
                    f"matching credit {txn.credit_paise}p within +/-{DEFAULT_TOLERANCE_PAISE}p, uniquely",
                ),
            ))
    return matches
