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
| 4 | L2 subset-sum solver | ✅ done |
| 5 | L3 agent (provider-agnostic) + L4 exception ledger | ✅ done (this repo) |
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

## Phase 5, part 1: a real gap fixed first

Designing L4's classifier surfaced that `bank_statement.csv`'s chargeback narration carried no reference back to its originating payment at all — legitimate and orphan chargebacks used the same template pool and a meaningless random reference, so they were textually identical. No engine, deterministic or LLM, could have told them apart from the public data sources as they stood; exposing the true link in `ground_truth.json` alone would only have fixed *scoring*, not given anything a real detection capability.

Fixed generator-side: `data/generator/ids.py::derive_dispute_ref(payment_id)` is a pure, deterministic transform embeddable in narration — the same "the engine can independently recompute this" pattern a settlement's UTR already uses, just applied to a different link. A legitimate chargeback's narration now embeds `derive_dispute_ref` of its real payment_id; an orphan chargeback (`defects.py`) gets a same-*shaped* but collision-checked random token, so format alone still can't distinguish them — only the lookup can. `engine/l0_deterministic.py::match_chargeback_payment` is the 4th L0 exact-key join this enables, and `ground_truth.json` gained a 4th link type (`chargeback_to_payment`) for scoring it. Verified purely additive against `run_2000`: auto-match and false-match rates unchanged; the new link type alone scores 100% recall, 0% false-match on its 8 legitimate chargebacks.

## Phase 5, part 2: L4 exception ledger

`engine/exceptions.py::classify_deterministic` is where "deterministic first" earns its keep for real: every category with an honest, generalizable rule is classified here, using only the four public CSVs plus `explain_variance` (built in Phase 3, unused until now) — `FEE_VARIANCE`/`TAX_VARIANCE` from `explain_variance`'s `FEE_TIER`/`GST_RATE` causes, `REFUND_MISALLOCATION`/`FX_VARIANCE` from a settled payment's gross disagreeing with its order's (refund_id present vs. international with none, respectively — the two injectors never touch the same field combination, so the categories never collide), `MISSING_SETTLEMENT`/`DUPLICATE_PAYMENT` from grouping captured payments by order and checking `settlement_id` presence, and `ORPHAN_CHARGEBACK`/`UNIDENTIFIED_CREDIT` from whatever L0-L2's residual still couldn't place.

`PERIOD_CUTOFF` is the one genuinely calibrated rule: a settlement's gap from its own payments' latest capture to its `settled_at` is empirically 1-4 days when normal and 3-31 days when genuinely cut off, with exactly one boundary case (3 days) indistinguishable from ~20 normal same-gap settlements using this signal alone. The threshold is set at >4 days: 13/14 caught, **zero false positives** — reported as one honest miss rather than 14/14 bought with ~20 false alarms.

A real scoring bug was caught verifying this against the eval harness: `eval/metrics.py::compute_defect_confusion` matched an exception to a defect by checking whether *any* value in the exception's `affected` dict overlapped the defect's identity, which let an unrelated `TAX_VARIANCE` exception's `settlement_id` *context* field get blamed for a different `PERIOD_CUTOFF` defect on the same settlement — reported as "misclassified" instead of the true "missed". Fixed with `CATEGORY_IDENTITY_FIELDS`, the symmetric counterpart to `DEFECT_IDENTITY_FIELDS`: matching now compares an exception's own *primary* identity field to a defect's identity, never an incidental context value.

Result on `run_2000`: **0 LLM calls, 132 exceptions**, every non-resolvable defect category covered (8 of 9 at 100% recall, `PERIOD_CUTOFF` at 13/14), every exception carrying a real `recommended_action` and non-empty `evidence_chain` — see [`benchmarks/phase5.json`](benchmarks/phase5.json).

## Phase 5, part 3: L3, provider-agnostic

The brief specs the Anthropic SDK (`claude-sonnet-5`). No Anthropic key was available when this was built; an NVIDIA NIM key was, and NIM serves open models (Llama, Nemotron, ...) via an OpenAI-compatible API, not Claude. Rather than couple the agent loop to one SDK's shape, `engine/llm/base.py` defines a minimal `LLMClient` protocol (`ToolSpec`, `ToolCall`, `Message`, `AssistantTurn`, one `complete()` method). `engine/llm/anthropic_client.py` implements it against the real Anthropic SDK and is unit-tested against a mocked client — spec-complete, ready the moment a key exists, but never run live. `engine/llm/nim_client.py` implements the same interface against NIM's OpenAI-compatible endpoint and is what actually ran for real, against `nvidia/nemotron-3-ultra-550b-a55b`. This is a documented deviation from the literal brief for the reference numbers, not a silent substitution.

`engine/l3_tools.py` implements the brief's 7 tools (`get_record`, `find_candidates`, `compute_expected_fee`, `explain_variance`, `solve_subset`, `propose_match`, `raise_exception`) with every hard constraint enforced structurally, not just prompted: a `ToolContext.known_ids` set rejects any id a tool didn't hand out (constraint 1); `compute_expected_fee`/`explain_variance` are the only arithmetic path (constraint 2); `confidence < 0.85` rejects `propose_match` outright (constraint 3); severity is recomputed from the amount via `severity_for_amount`, never trusted from the model, and a match over ₹50,000 auto-emits a `HIGH_VALUE_MATCH_REVIEW` companion exception regardless of confidence (constraint 4); `propose_match`'s rationale must cite a known id (constraint 5, best-effort); and `engine/l3_agent.py`'s loop itself raises an `AGENT_INCOMPLETE` exception on a record's behalf if it exhausts its turn budget without closing the loop (constraint 6, enforced at the infrastructure level — never a silent drop).

`run_2000`'s residual for L3 is empty (L0-L2 and L4's deterministic classifier already resolve everything), so L3's real capability was proven on a small hand-built synthetic exercise set (`engine/l3_agent.py --profile`) instead — 5 records engineered to need `UNEXPLAINED_VARIANCE` reasoning, a clean high-value match, weak/uncertain evidence, a genuine 2-settlement batch requiring `find_candidates` → `solve_subset` chaining, and an unresolvable ambiguity. Run for real:

- **A genuine false-match bug was found and fixed before the reference run.** The model correctly traced a payment → its settlement → the settlement's own bank credit (excellent, fully-evidenced reasoning) and bundled all three ids into one `propose_match` call. `_infer_link_type` inferred a link type from table names alone, so any `(payment, bank)` pair became `chargeback_payment` regardless of whether that bank row was actually a debit — here it wasn't, so the tool asserted a chargeback that never happened. Fixed to check the bank row's actual `debit_paise`; a credit is now rejected with an error explaining the real scope boundary (`propose_match` only asserts links to the record under investigation). Regression-tested in `tests/test_l3_tools.py` before re-running live.
- **The clean re-run: zero false matches.** `pay_unexplained` and `pay_ambiguous` correctly classified/refused; `btxn_batch` correctly chained `find_candidates` → `solve_subset` → `propose_match` across both settlements, with the `HIGH_VALUE_MATCH_REVIEW` companion firing automatically; `pay_high_value` and `pay_weak_evidence` hit `AGENT_INCOMPLETE` after thorough, correct-but-unfinished investigation exhausted the 8-turn budget - constraint 6's fallback working exactly as designed, not a false positive, but a real signal the turn budget is tight for a genuinely uncertain case.

Committed as [`benchmarks/phase5_synthetic.json`](benchmarks/phase5_synthetic.json), full traces in [`benchmarks/sample_traces/`](benchmarks/sample_traces/).

## Phases 6–7 (design intent, not yet built)

- **Cash** (`cash/`): unsettled-payment SLA forecasting, a 14-day inflow curve, and reconciled-vs-book cash where the delta is fully explained by the exception ledger.
- **Dashboard**: the layer waterfall, exception queue with drill-down to the agent trace, cash position — or the `eval/report.py` HTML fallback if time runs out.

Full detail in [`KOSH_BUILD_PROMPT.md`](KOSH_BUILD_PROMPT.md).
