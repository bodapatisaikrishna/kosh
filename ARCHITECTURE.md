# Kosh — Architecture

## The problem

Three sources must tie out to the paisa:

| Source | What it is | Key fields |
|---|---|---|
| A. Orders | Merchant's ERP/invoice export | `order_id`, gross amount, date |
| B. PG ledger | Payment gateway transaction + settlement reports | `payment_id`, `order_id`, gross, MDR fee, GST, net, `settlement_id`, `utr` |
| C. Bank statement | Bank credits with free-text narration | `value_date`, `credit_paise`, `narration` (UTR buried in it) |

The hard part: one bank credit is a **batch**. It equals the sum of many payments' net amounts, minus refunds, minus chargebacks, plus/minus adjustments. When the UTR in the narration is mangled or missing, the system must *solve* for which set of payments explains that credit.

## Design rule: deterministic first, LLM last

An LLM doing arithmetic across thousands of rows is slow, expensive, and wrong. Kosh is a layer cake, and each layer only ever sees what the layer above it couldn't resolve:

```
┌─────────────────────────────────────────────────────────────┐
│ L0  Deterministic joins   (exact keys)          ~85% target │
├─────────────────────────────────────────────────────────────┤
│ L1  Tolerance matching    (±amount, ±date)       ~8% target │
├─────────────────────────────────────────────────────────────┤
│ L2  Combinatorial solver  (subset-sum)           ~4% target │
├─────────────────────────────────────────────────────────────┤
│ L3  Claude agent          (residual only)        ~3% target │
├─────────────────────────────────────────────────────────────┤
│ L4  Exception ledger      (honest remainder)   whatever's left│
└─────────────────────────────────────────────────────────────┘
```

The agent never sees the ~97% that deterministic code already handled. That's the whole competitive story, and it's also just correct engineering: cheap, fast, auditable layers absorb everything they can before anything nondeterministic runs.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Synthetic data generator + ground truth | ✅ done |
| 2 | Eval harness (metrics vs. ground truth) + null/oracle baselines | ✅ done |
| 3 | L0 deterministic + L1 tolerance matching | ✅ done |
| 4 | L2 subset-sum solver | ✅ done (this repo) |
| 5 | L3 Claude agent + L4 exception ledger | not started |
| 4 | L2 combinatorial (subset-sum) solver | not started |
| 5 | L3 Claude agent + L4 exception ledger | not started |
| 6 | Cash position + dashboard + benchmark freeze | not started |
| 7 | README/architecture/demo polish for submission | not started |

## Phase 1: the data generator

`data/generator/` builds one internally-consistent "clean world" (orders → payments → refunds → settlements → bank statement, all tying out exactly), then mutates it with 12 configurable defect injectors (`defects.py`). Ground truth is recorded from the *pre-mutation* clean world plus a labelled record of every mutation, so `eval/` (Phase 2) can score an engine's output against a graph that is true by construction, not by inference.

Key modules:

- `fees.py` — the single MDR + GST model, integer-only, imported unchanged by `engine/` in later phases so a fee bug shows up as a failing test, not a silently-accepted false match.
- `world.py` — builds the clean, defect-free dataset.
- `defects.py` — the 12 injectors; see [README.md](README.md#the-12-injected-defect-types).
- `emit.py` — serializes to the four CSV schemas + `ground_truth.json` + `manifest.json`.
- `trace.py` — hand-verification CLI: bank credit → settlement → payments → orders, with a `TIES OUT` / `OFF BY` verdict.

See [README.md](README.md) for the reference-run numbers and reproduction steps.

## Phase 2: the eval harness

`engine/contract.py` defines the interface every layer built in Phases 3–5 must speak: an `EngineOutput` of `Match`es (`link_type`, `left_id`/`right_id`, `layer`, `confidence`, `evidence`) and `ReconException`s (`category`, `severity`, `amount_at_risk_paise`, `affected`, `recommended_action`). Nothing downstream — `eval/`, the future dashboard, Phase 5's agent tools — touches raw CSV rows directly; everything speaks this contract.

`eval/metrics.py` scores an `EngineOutput` against `ground_truth.json`: throughput (records/sec, LLM cost in integer micro-dollars), accuracy (auto-match rate, per-layer contribution, false-match rate, precision/recall, per-defect-class confusion matrix), and an honest exception summary (count, ₹ at risk, category/severity breakdown). `eval/report.py` emits a timestamped JSON + a self-contained `report.html` per run.

Two baseline "engines" (`engine/baselines.py`) exist purely to validate the harness before any real matching logic is written:

- **`null_baseline`** asserts nothing and raises one exception per captured payment → must score 0% auto-match, 0% recall, 0.00% false-match, 100% of records exceptioned.
- **`oracle_baseline`** reads `ground_truth.json` directly and asserts exactly the true link graph → must score ~100% precision/recall, 0.00% false-match, and an exception ledger that exactly matches the unresolvable defects (no misses, no misclassifications, and — critically — no false exceptions raised over the 3 defect types that should resolve silently).

Both are frozen in `tests/baselines/*.json` as a regression guard: if the scoring logic in `eval/metrics.py` drifts, or a baseline's own behavior drifts, `pytest` fails immediately. **`false_match_rate` is the metric this whole harness is built to protect** — it's computed against the engine's own assertions (wrong ÷ total asserted), not against total records, specifically so it can't be gamed by asserting fewer links.

Key design point an engine must respect from Phase 3 onward: **`engine/io.py` never reads `ground_truth.json`.** Only `eval/io.py` and the oracle baseline do — the oracle exists to test the scorer, not to reconcile anything, and a real engine that read ground truth would invalidate every accuracy number this project exists to produce.

## Phase 3: L0 deterministic + L1 tolerance

`engine/l0_deterministic.py` runs the exact-key cascade in three steps: `orders.order_id ↔ pg_payments.order_id` and `pg_payments.settlement_id ↔ pg_settlements.settlement_id` are direct, unambiguous FK joins (confidence 1.0, always). The third step, `pg_settlements.utr ↔` a UTR pulled out of `bank_statement.narration`, is where real ambiguity can arise: `engine/normalize.py` extracts a UTR-shaped token (`[A-Z]{4}[NR]\d{1,11}`) from free text and classifies it as a full 16-char exact match, a ≥12-char truncated prefix, or nothing usable. A full match does an O(1) dict lookup against known settlement UTRs; a prefix match requires the prefix to identify *exactly one* settlement — two or more candidates means it's refused, not guessed, and falls through to L1.

`engine/l1_tolerance.py` picks up whatever L0 couldn't place — in practice, a bank credit whose UTR was rekeyed with a transposed digit (still 16 chars, still syntactically valid, but matching no real settlement). It requires all three: amount within ±300 paise, date within ±3 days, and a narration-similarity score ≥0.80 against a settlement-vocabulary bag (`engine/normalize.py:settlement_narration_similarity`) that distinguishes a genuine (even truncated) settlement narration from a customer-name or chargeback one. The same rule applies: more than one settlement satisfying all three tolerances means the candidate is ambiguous and is refused, never picked by "closest".

A subtlety worth naming: a bank row is excluded from settlement-matching only when it's a genuine debit (`debit_paise > 0`), never merely because `credit_paise == 0` — a settlement can legitimately net to exactly ₹0 (e.g. after a `period_cutoff` shift on a small settlement), and that's still a real credit leg, not a debit. An earlier version of `l0_deterministic.py` filtered on `credit_paise <= 0` and silently dropped these; caught by the reference-fixture recall dropping to 99.94% instead of 100%, which is exactly why every checkpoint is measured against `run_2000`, not eyeballed.

`engine/fees.py` re-exports the generator's `compute_expected_fee` unchanged and adds `explain_variance(observed, expected, ...)`, which decomposes a paise delta into `MATCH` / `ROUNDING` / `FEE_TIER` / `GST_RATE` / `REFUND`, or honestly returns `UNEXPLAINED` when none of those hypotheses fit. This function does no matching itself — Phase 3's pipeline (`engine/pipeline.py`) doesn't call it yet — but it's the piece Phase 5's exception ledger and Phase 5's agent tools both build on, so it's built and tested now.

Result on `data/fixtures/run_2000`: 97.85% auto-match, 100.00%/100.00% precision/recall, **0.00% false-match**, ~4ms wall clock — see [`benchmarks/phase3.json`](benchmarks/phase3.json). Phase 3's pipeline doesn't raise any exceptions yet (L2/L3/L4 don't exist), so whatever L0/L1 can't place is simply absent from the match list for now, not silently claimed as reconciled.

## Phase 4: L2 subset-sum solver

`engine/l2_subset.py` handles the "one bank credit is a batch" problem literally: given a credit L0/L1 couldn't place, and a pool of settlements within a same-day/T+1 date window (excluding any settlement already confidently matched elsewhere), find the subset whose net amounts sum to it within tolerance.

Both algorithms the spec suggests have a scaling trap specific to this domain. Meet-in-the-middle costs O(2^(n/2)) in the candidate count — dangerous at the spec's own `max_terms=40`. A bitset DP (one Python bigint, one bit per achievable paise value) is the classic alternative, and it's genuinely cheap as a one-off feasibility check — but reusing it *per DFS node* during subset recovery costs O(bit-length) at every node, and bit-length here tracks money magnitude (paise), not candidate count. This was caught by profiling, not by inspection: an early version comfortably passed correctness tests, then blew the 250ms budget (p99 over 1.2s) on nothing more exotic than 40 candidates around ₹50,000 each, because every prune check was building or shifting a multi-megabit integer.

The shipped solver drops the bitset entirely and prunes with a plain O(1) sum-bound at every DFS node (the most any remaining branch could still add) — weaker pruning in theory, but its cost depends only on n, so a node-count-based time check gives a real, predictable worst case regardless of how large the amounts are. Result: **p50 ~2ms, p99 ~50ms, max ~101ms** over 2,000 synthetic instances at n up to 40 (`benchmarks/phase4_solver_perf.json`) — comfortably inside the 250ms cap with margin, not hugging it.

The ambiguity guard mirrors L0/L1's: more than one distinct subset satisfying the target returns `AMBIGUOUS` with the alternatives found (capped at 2 — "more than one exists" is all that matters), never a pick. A pool over `max_terms`, or a search that exhausts its time budget, both escalate the same way as a genuine `NONE` — this layer simply declines to match, leaving the credit for L3/L4.

**On `data/fixtures/run_2000`, L2 contributes zero matches.** L0+L1 already achieve 100% recall on every tie-outable `settlement_bank_txn` link — a split settlement resolves via both credits sharing one UTR (L0), and a mangled UTR resolves via amount+date+narration tolerance (L1) — so nothing genuinely batched-and-unrecoverable reaches this layer in the current defect suite. Rather than manufacture a residual to make L2 look busy, this is reported as-is: L2's correctness (a genuine multi-settlement batch, the ambiguity guard, the already-matched exclusion, the date-window prune) and its timing are verified directly, by `tests/test_l2_subset.py` and `tests/test_l2_subset_perf.py` respectively, independent of whether this dataset happens to invoke it.

## Phases 5–7 (design intent, not yet built)

- **L3**: a Claude agent restricted to a fixed toolset (`get_record`, `find_candidates`, `compute_expected_fee`, `explain_variance`, `solve_subset`, `propose_match`, `raise_exception`), structurally forbidden from asserting an ID it wasn't handed by a tool, and forbidden from doing its own arithmetic.
- **L4**: an exception ledger sorted by ₹ at risk — every unresolved item, no suppression.
- **Cash** (`cash/`): unsettled-payment SLA forecasting, a 14-day inflow curve, and reconciled-vs-book cash where the delta is fully explained by the exception ledger.

Full detail in [`KOSH_BUILD_PROMPT.md`](KOSH_BUILD_PROMPT.md).
