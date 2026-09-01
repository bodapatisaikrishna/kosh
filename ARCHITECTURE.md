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
| 5 | L3 agent (provider-agnostic) + L4 exception ledger | ✅ done |
| 6 | Cash position + dashboard + benchmark freeze | ✅ done |
| 7 | README/architecture/demo polish for submission | ✅ done (this repo) |

## Phase 1: the data generator

`data/generator/` builds one internally-consistent "clean world" (orders → payments → refunds → settlements → bank statement, all tying out exactly), then mutates it with 12 configurable defect injectors (`defects.py`). Ground truth is recorded from the *pre-mutation* clean world plus a labelled record of every mutation, so `eval/` (Phase 2) can score an engine's output against a graph that is true by construction, not by inference.

Key modules:

- `fees.py` — the single MDR + GST model, integer-only, imported unchanged by `engine/` in later phases so a fee bug shows up as a failing test, not a silently-accepted false match.
- `world.py` — builds the clean, defect-free dataset.
- `defects.py` — the 12 injectors, detailed below.
- `emit.py` — serializes to the four CSV schemas + `ground_truth.json` + `manifest.json`.
- `trace.py` — hand-verification CLI: bank credit → settlement → payments → orders, with a `TIES OUT` / `OFF BY` verdict.

### Reference run (`data/fixtures/run_2000`, seed 42, 3 months)

| Metric | Value |
|---|---|
| Orders | 2,000 |
| PG payments (incl. injected duplicates) | 2,020 |
| Settlements | 305 |
| Bank statement rows | 351 |
| GMV | ₹14,779,996.39 |
| Injected defects | 188 (~9.4% of orders), all 12 types present |

A small companion fixture, `data/fixtures/sample_200` (seed 7, 1 month), is committed to the repo so real output is inspectable without running the generator.

### The fee model

Realistic Indian PG economics, so failures concentrate where they should:

| Method | MDR |
|---|---|
| UPI | 0.00% (zero-MDR P2M — real regulatory position) |
| RuPay debit | 0.00% |
| Card (domestic) | 2.00% |
| Netbanking | 1.90% |
| Wallet | 2.00% |
| Card (international) | 3.00% |

Zero-MDR UPI/RuPay means most of that volume reconciles trivially (`net == gross`), which is intentional, not a shortcut: the interesting defects (fee-tier errors, GST variance, FX drift) concentrate in card and international volume, exactly like a real merchant's exception queue. GST is 18% of the fee, and rounding is integer half-up applied at each step (fee first, then GST on the rounded fee), not once at the end — that ordering produces genuine ±1 paisa drift, one of the 12 defect types and also just how real gateways compute it. All money is integer paise, enforced by an AST-level lint (`tests/test_no_floats.py`) that fails the build on a stray `/`, `round()`, or float literal in the fee/settlement arithmetic.

### The 12 injected defect types

| # | Defect | Expected exception category |
|---|---|---|
| 1 | Payment captured but never settled | `MISSING_SETTLEMENT` |
| 2 | Duplicate payment row | `DUPLICATE_PAYMENT` |
| 3 | Paisa rounding drift (±1–3p) | *resolvable — not an exception* |
| 4 | Wrong MDR tier applied | `FEE_VARIANCE` |
| 5 | GST ≠ 18% of fee | `TAX_VARIANCE` |
| 6 | Refund booked against wrong order | `REFUND_MISALLOCATION` |
| 7 | Chargeback debit with no linked payment | `ORPHAN_CHARGEBACK` |
| 8 | Settlement lands after month-end cutoff | `PERIOD_CUTOFF` |
| 9 | UTR truncated/mangled in narration | *resolvable via subset-sum — not an exception* |
| 10 | International payment with FX rate drift | `FX_VARIANCE` |
| 11 | Non-PG bank credit (direct customer NEFT) | `UNIDENTIFIED_CREDIT` |
| 12 | One settlement split across two bank credits | *resolvable via subset-sum* |

Three of the twelve are marked *resolvable*: they exist specifically to test that a reconciliation engine is smart enough **not** to raise a false exception over ordinary rounding drift, a truncated bank narration, or a split settlement batch.

## Phase 2: the eval harness

`engine/contract.py` defines the interface every layer built in Phases 3–5 must speak: an `EngineOutput` of `Match`es (`link_type`, `left_id`/`right_id`, `layer`, `confidence`, `evidence`) and `ReconException`s (`category`, `severity`, `amount_at_risk_paise`, `affected`, `recommended_action`). Nothing downstream — `eval/`, the future dashboard, Phase 5's agent tools — touches raw CSV rows directly; everything speaks this contract.

`eval/metrics.py` scores an `EngineOutput` against `ground_truth.json`: throughput (records/sec, LLM cost in integer micro-dollars), accuracy (auto-match rate, per-layer contribution, false-match rate, precision/recall, per-defect-class confusion matrix), and an honest exception summary (count, ₹ at risk, category/severity breakdown). `eval/report.py` emits a timestamped JSON + a self-contained `report.html` per run.

Two baseline "engines" (`engine/baselines.py`) exist purely to validate the harness before any real matching logic is written:

| Baseline | What it does | auto-match | precision / recall | false-match | exceptions |
|---|---|---|---|---|---|
| `null` | asserts nothing | 0.00% | 0.00% / 0.00% | 0.00% | 100% of records |
| `oracle` | reads `ground_truth.json` directly | 97.85%* | 100.00% / 100.00% | **0.00%** | 133 (exactly the unresolvable defects) |

\* Not 100%: payments hit by `missing_settlement` or `duplicate_payment` have no true settlement link to match — they correctly land on the exception ledger instead.

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

## Phase 6, part 1: cash/forecast.py

Settlement SLA reuses `data.generator.calendar.settlement_date` unchanged — the same one-source-of-truth pattern as `engine/fees.py` re-exporting `compute_expected_fee`. `compute_inflow_curve` buckets unsettled captured payments' net by their own method's expected settlement date over the next 14 days; `compute_stuck` flags anything past its own SLA + 1 day grace, still unsettled — distinct from a payment still legitimately in flight.

The hard part is `compute_cash_reconciliation`: book cash (accrual, gross on every captured payment) vs. reconciled cash (cash basis, actual net bank movement) must tie **to the paisa**, per the brief. Verified the same way every identity in this project has been — computed against `run_2000`, and iterated until the residual hit exactly zero, not assumed correct from the derivation alone:

- First pass was off by ~₹3.1 lakh. Cause: `unsettled_gross_paise` was summed on `net_paise` instead of `gross_paise` — an unsettled payment hasn't been through the fee-deduction pipeline at all, so its *full gross* belongs in the gap, not a net figure that doesn't apply yet.
- Second pass was off by exactly -18 paise. Cause: the fee/GST term was reconstructed as `fee_paise + gst_paise`, which silently drops `rounding_drift` (applied directly to `net_paise`, never touching the fee/GST fields). Replaced with `gross_paise - net_paise` directly — the true source of truth for what a settlement actually paid out, regardless of which defect caused the gap.
- Also fixed: sign errors on `adjustments_paise` and `unidentified_credit_paise` (both *increase* actual bank cash, so they should *shrink* the book-reconciled gap, not widen it), and a missing `legitimate_chargeback_paise` term entirely, found via L0's `chargeback_payment` matches.

Residual is now exactly 0 on both `run_2000` and `sample_200`. Separately, `_infer_as_of_date` originally took the max date across payments+settlements+bank — since settlements/bank trail captures by the T+N lag, that pushed "today" past every unsettled payment's own expected date and made the 14-day curve trivially all-zero. Fixed to use only capture dates.

## Phase 6, part 2: dashboard + benchmark freeze

Per the brief's own fallback rule ("a working CLI + report.html beats a broken dashboard, judges grade accuracy not CSS"), the 4 panels live in `eval/report.py`'s existing static HTML report rather than a new Next.js subsystem:

1. **Headline strip** — gained "₹ reconciled" (previously missing).
2. **Layer waterfall** — unchanged from Phase 2.
3. **Exception queue** — now sortable (click the Category header; vanilla JS, no framework) and click-to-expand per row, showing the full `evidence_chain`, `affected` ids, `recommended_action`, and a link to the agent trace when one exists (checked against `benchmarks/sample_traces/` — the 5 live L3 records have one, every `run_2000` exception is deterministically classified and correctly shows none).
4. **Cash position** — a 14-day inflow bar chart plus the full book-vs-reconciled table, rendered as the named components from Part 1, which sum exactly.

Verified interactively in the browser, not just by reading the HTML source: bar heights checked against the underlying data via DOM inspection, click-to-expand confirmed to actually toggle the detail row's computed `display` style, and the sort function confirmed to actually reorder rows by amount.

**Benchmark freeze**: `data/fixtures/run_{500,2000,10000}` (seed 42) through the complete pipeline, committed as `benchmarks/freeze_{500,2000,10000}.{json,html}` — consistent across a 20x size range (see README's results table), wall clock scaling linearly.

**Fresh-clone checkpoint verified for real**: cloned into an isolated temp directory, `pip install -e .` into a brand-new venv, `make demo` ran clean. That same run caught a real gap — `pytest` crashed at *collection* (not just failed) because `test_anthropic_client.py`/`test_nim_client.py` import `anthropic`/`openai` unconditionally, and those packages live in the optional `llm` extra, not the base install. Fixed with `pytest.importorskip`: a bare install now gets 2 graceful skips (140 passed, 2 skipped) instead of an interrupted run; the dev environment with the `llm` extra is unaffected (151/151).

## Hardening: making L2 and L3 earn their place

The layer cake's whole claim is that each layer absorbs what the one above it can't. On the original fixture that claim was **unproven for half the stack**: L2 and L3 both contributed exactly zero. That was reported honestly, but honesty about an unproven claim is not the same as proving it — and the fault was a generator that was too kind, not an engine that was too good. Two injectors were added, both modelled on what real gateways actually do:

**13. `consolidated_payout`** — one bank credit paying 2–4 same-day settlements at once, carrying only a batch reference (`PYT…`, deliberately not UTR-shaped) and no per-settlement UTR. Real daily payout runs look exactly like this. L0 structurally cannot join it (no UTR to key on); L1 cannot either (no single settlement's amount matches a 2–4 way sum). Only subset-sum can explain it.

**14. `compound_fee_tax_error`** — a payment where the fee tier *and* the tax rate are both wrong at once. `explain_variance`'s fee-tier hypothesis assumes correct GST and its GST-rate hypothesis assumes the correct fee, so neither can decompose a delta caused by both. The injected rates (1150/1375/2550 bps) are deliberately disjoint from `KNOWN_WRONG_GST_BPS`, so the GST branch cannot accidentally catch it. The result is a genuinely `UNEXPLAINED` variance — real work for L3, and verified to be exactly that: the residual set equals the injected set, no false residual and none missed.

Two things worth naming about *how* this was done, because both cut against making the numbers look good:

- **The L1 test was made stricter, not weaker.** A consolidated credit initially scored 0.71 narration similarity, so L1 rejected it — but for the wrong reason (unfamiliar wording). `CONSOLIDATED`/`CONSOL`/`PAYOUT` were added to the settlement vocabulary (genuine domain wording any ops team reads as a settlement credit), raising it to 0.86 so it now *passes* L1's gate. L1 must therefore reject it on **amount** alone, which is the real reason L2 is required.
- **A degenerate ambiguity was fixed properly rather than papered over.** Two consolidated payouts returned `AMBIGUOUS` because one member settlement nets to exactly ₹0 — including or excluding a zero-value term gives an identical total, so *k* such terms multiply the solution count by 2^*k* while carrying no information. The guard was refusing correctly but uselessly. `l2_subset` now excludes zero-amount candidates from the pool, so the meaningful members resolve unambiguously and the ₹0 settlement is simply never claimed. That costs 2 links of recall (99.95%, not 100%) and is the honest answer: a bank credit genuinely cannot evidence whether a ₹0 settlement rode along inside it.

This also exposed a real test bug I had previously mis-diagnosed. `test_run_l3_aggregates_matches_and_exceptions_across_records` had failed intermittently once, and I attributed it to my own edit script rewriting a file mid-import. **That was wrong.** `run_l3` executes records concurrently, and the test scripted a single shared `FakeClient` queue — so which record received which scripted turn was a genuine race. It only surfaced now because the `propose_match` false-match fix made the unfavourable interleaving fail loudly instead of silently asserting a bad link. `FakeClient` gained per-record scripting and a lock; the test is now deterministic.

## L3 against the real residual, live

Hardening gave L3 a genuine, non-empty `run_2000` residual for the first time (6 records). Running it for real against `nvidia/nemotron-3-ultra-550b-a55b` surfaced a real production-shape gap before it produced a real result:

**A single 504 killed the whole batch.** The first live attempt died with `openai.InternalServerError: Error code: 504` from inside `asyncio.gather` — one upstream 5xx, and every sibling record's result was discarded with it, not just the one that failed. `_call_with_backoff` only retried `RateLimitedError` (429); a 5xx/timeout/connection error wasn't a case it had been built for at all. Fixed in two parts, deliberately kept separate: (1) `TransientBackendError`, a new exception distinct from `RateLimitedError` — translated from `InternalServerError`/`APITimeoutError`/`APIConnectionError` in both `NimClient` and `AnthropicClient` — retried with the same backoff, because a 5xx means the request was never meaningfully processed and retrying it is safe; (2) each record's `run_one()` task wrapped in its own try/except so *any* failure, retried-out or not, becomes an honest `AGENT_INCOMPLETE` ledger entry rather than destroying the other five records' results. Two new tests cover both paths directly: a client that fails once then succeeds (proves the retry), and a client that always fails for one specific record while succeeding for another (proves isolation).

**The isolation fix then proved itself against a harder failure than the one it was built for.** The model started returning `NotFoundError: 404` mid-session — not a 5xx, a "this model doesn't exist right now" from NIM's side, confirmed by a direct minimal call outside the harness while `models.list()` still listed it in the catalog: a transient provider-side outage, not a config error. 404 is deliberately *not* retried (retrying a genuinely-gone resource wastes budget for nothing), so all 6 records correctly fell through to `AGENT_INCOMPLETE` with the real reason recorded — the batch didn't crash, it produced 6 honest ledger entries. Re-run after the outage cleared, the same model returned real results.

**The first clean result, independently checked against `ground_truth.json` directly rather than trusted from the pipeline's own summary**: 2 of 6 records correctly matched — both cross-checked by hand against `ground_truth.json`'s `order_to_payment` links — and 4 of 6 ended `AGENT_INCOMPLETE`, exhausting the 8-turn budget. Zero false matches. Checking *why* 4 of 6 ran out of budget, rather than accepting the number, found a real cause and three more real bugs, none of which the summary numbers alone would have surfaced.

**The turn budget was actually 1 turn short, not just "tight."** Reading all four `AGENT_INCOMPLETE` traces turn-by-turn: three showed the same shape — `get_record`, `compute_expected_fee`, three separate `explain_variance` calls (correctly probing fee, GST, and net variance independently, because this is `compound_fee_tax_error`, where no single hypothesis decomposes the delta), two more `get_record` calls, then `find_candidates` on the very last turn to rule out a batching explanation - a completely legitimate investigation that simply needed one more turn to call `raise_exception`. The fourth had already reasoned correctly and tried to close the loop, but `propose_match` rejected its argument (it tried to assert a bank-credit id that only relates to the settlement, not the payment under investigation) on the last available turn, with no room left to retry with a corrected argument. `DEFAULT_MAX_TURNS` raised from 8 to 12. While making this change, a second latent bug surfaced: `_cache_key()` never included `max_turns`, only `PROMPT_VERSION` - changing the budget without remembering to bump that constant would have silently replayed the old, incomplete cached trace forever. Fixed by adding `max_turns` to the key directly, so this class of bug can't recur regardless of what a future change remembers to bump.

**Re-running with the new budget surfaced two more real bugs, both about ledger fidelity, not link correctness.** `raise_exception` accepted whatever category string the model supplied with zero validation; the live model invented `FEE_CALCULATION_VARIANCE` and `unexplained_fee_variance` - neither a real category - which would have silently broken every category-keyed downstream consumer (the dashboard's by-category breakdown, `compute_defect_confusion`'s exact-match scoring) with no error anywhere. Fixed the same way every other hard constraint in this file is enforced: structurally, not just prompted - `category` is now an `enum` in the tool's own JSON schema, and `raise_exception` rejects anything outside `RECOMMENDED_ACTIONS`'s keys with a `ToolError`, giving the model a real chance to retry within its remaining turns instead of polluting the ledger. Separately, one exception reported `amount_at_risk_paise: 58700` - the payment's full gross - when its own rationale text correctly identified the actual variance as 142 paise; the tool schema's description of that field said nothing about what it should mean, so it was clarified explicitly ("the specific unexplained amount... NOT the payment's full gross/net amount").

**Re-running again to exercise both fixes found a fourth, deeper bug, this time in the scoring code itself, not the agent.** `raise_exception` had always stamped `affected` as `{"record_id": ..., "source": ...}` only - a different convention from `engine/exceptions.py`'s own, which always uses the source-specific field (`payment_id`, `bank_txn_id`, ...). `eval/metrics.py`'s `CATEGORY_IDENTITY_FIELDS` looks up exceptions by that source-specific field for every category except `UNEXPLAINED_VARIANCE` (which happens to check `record_id` too). The result: every `FEE_VARIANCE`/`TAX_VARIANCE`/etc. exception L3 ever raised was **invisible** to `compute_defect_confusion` - not misclassified, not detected, just silently absent, indistinguishable from "nothing was raised at all." This had been true since Phase 5 and nothing had ever surfaced it, because no one had checked the live run's own `defect_confusion` breakdown against its exception list by hand until now. Fixed by having `raise_exception` stamp the source-specific field too (reusing `get_record`'s own `_ID_FIELD` mapping, the same fix applied to the failed-record fallback path in `l3_agent.py`), and by giving `AGENT_INCOMPLETE` its own `CATEGORY_IDENTITY_FIELDS` entry so a turn-budget exhaustion now reads as "misclassified" (something was raised, wrong label) rather than "missed" (nothing was raised) - a materially more honest signal for a reader of the per-defect-class table. Three tests cover this directly, exercising the real `raise_exception` function rather than hand-built exception objects.

**The final, fully-verified result** ([`benchmarks/phase5_live_residual.json`](benchmarks/phase5_live_residual.json), traces in [`benchmarks/sample_traces_live/`](benchmarks/sample_traces_live/)), independently checked field-by-field against `ground_truth.json` rather than trusted from any summary number: 0 of 6 `AGENT_INCOMPLETE`, 0 invented categories, 0 false matches. 5 of 6 exception amounts matched the generator's own labelled defect amount exactly to the paisa; the sixth cited the largest single unexplained leg's delta (₹7.44) rather than the smaller net delta (₹1.83) when two legs partially offset - a real but minor imprecision, documented rather than hidden (see `defect_confusion_note` in the committed JSON and README's Limitations). `compute_defect_confusion` now honestly shows 1 detected / 4 misclassified / 1 missed for `compound_fee_tax_error`, replacing a `{detected: 2, missed: 4}` reading that had been silently wrong the whole time. This took three live re-runs and four real bugs, on top of the two already found in the section above - six in total from one 6-record residual, none of which a false-match-rate or auto-match-rate summary alone would ever have caught.

**Cost, checked against Phase 5's own "<$0.50/1000 records" checkpoint, and found to not exist at all.** `EngineMeta.cost_usd_micros` existed in the contract and was threaded through `eval/metrics.py`'s `cost_per_1000_records` calculation, but nothing ever *set* it - every committed benchmark, including live runs with real tokens billed against a real account, reported `cost_usd_micros: 0`. Token counts were tracked correctly throughout; only the USD conversion was missing, silently, since Phase 5. Fixed with `engine/llm/pricing.py`: a small per-model $/1M-token table (NIM's rate for `nvidia/nemotron-3-ultra-550b-a55b` sourced from its public OpenRouter listing, since NVIDIA doesn't publish per-token pricing for the hosted NIM preview endpoint itself; Anthropic's official published rates included for `AnthropicClient`, never run live) feeding a `cost_usd_micros(model, input_tokens, output_tokens)` function, wired through `l3_agent.py`'s per-trace and aggregate cost, `pipeline.py`'s meta construction, and a new headline card in `eval/report.py` (shown only when `llm_calls > 0`, so a deterministic-only run never shows a misleading "$0.00"). The final run's real number: **$0.102 total, $0.055 per 1000 records** - comfortably inside the brief's target even at the higher 12-turn budget and the extra tokens six re-runs cost, but the point was never that it passed; it's that it couldn't have been checked at all before this fix.

## A third silently-always-zero field: aging_days

Same bug shape as `cost_usd_micros`, found by the same discipline: don't trust a field just because it's in the contract - check whether anything actually sets it. `ReconException.aging_days` is part of the brief's own exception-ledger shape (`category · severity · amount_at_risk · aging_days · evidence_chain · recommended_action · suggested_owner`) and is rendered as its own "Aging" column in the dashboard's exception queue - every row read `0d`, for every exception, at every scale, the whole time. Nothing in `engine/exceptions.py` or `engine/l3_tools.py` had ever computed it; every `ReconException` construction site either omitted the field (defaulting to 0) or, in one case, took a `d["aging_days", 0]` fallback that only ever had 0 to fall back to.

Fixed by giving both producers a real, shared notion of "now" and "when did this happen": `engine/io.py` gained `infer_as_of_date(dataset)` (moved from `cash/forecast.py`'s private, near-identical copy - one source of truth for what "today" means for a given fixture, the same principle as `compute_expected_fee`) and `aging_days(as_of, event_date_str)`. Every deterministic classifier in `engine/exceptions.py` now passes the record's own underlying event date - a payment's `captured_at` for `MISSING_SETTLEMENT`/`FEE_VARIANCE`/`REFUND_MISALLOCATION`/etc, a settlement's `settled_at` for `PERIOD_CUTOFF`, a bank line's `value_date` for `ORPHAN_CHARGEBACK`/`UNIDENTIFIED_CREDIT`. `engine/l3_tools.py`'s `raise_exception` and its `HIGH_VALUE_MATCH_REVIEW` companion do the same via `_residual_aging_days`, looking up the record under investigation's own date field. `aging_days` is clamped at 0 (never negative - a negative value would only mean a data-quality issue in the fixture, not something a ledger should ever display).

This surfaced a second, smaller bug while fixing the first: `infer_as_of_date` crashed with `max() arg is an empty sequence` on a dataset with zero payments - true for two existing unit tests that construct a bank-only `Dataset` to isolate `ORPHAN_CHARGEBACK`/`UNIDENTIFIED_CREDIT` classification. Rather than coupling every isolated classifier test to a full payments list, `infer_as_of_date` was made to fall back to the latest settlement/bank date, and finally to the real wall-clock date only if the dataset carries no dates at all - a degenerate case that only a synthetic test fixture, never a real one, would hit.

Purely additive: every committed benchmark's `auto_match_rate`/`false_match_rate`/`precision`/`recall`/exception count is byte-identical before and after (verified by diff, not assumed) - only `aging_days` (and the naturally-varying `wall_clock_seconds`/`generated_at_unix`) changed. `benchmarks/freeze_{500,2000,10000}.json`, `phase3.json`, `phase4.json`, `phase5.json`, and both baselines regenerated and re-verified.

## A fourth: suggested_owner, real but never differentiated

Prompted by finding three "field exists, nothing sets it" bugs above, a deliberate sweep of every field in `engine/contract.py` turned up a fourth, quieter version of the same shape: `ReconException.suggested_owner` was never *unset* (it always had its dataclass default, `"Reconciliation Ops"`), but nothing ever *overrode* it either - every category, at every scale, showed the identical owner. Not wrong, exactly, but a column that carries zero information is the same failure as one that's silently 0: a real ops org doesn't route an FX rate dispute and a chargeback dispute to the same desk, and a judge scrolling the exception queue would see one unchanging value in a column that exists specifically to answer "who handles this."

Fixed with `SUGGESTED_OWNERS`, a category-to-team map in `engine/contract.py` next to `RECOMMENDED_ACTIONS` (same pattern, different question - "who" instead of "what"): `MISSING_SETTLEMENT`/`DUPLICATE_PAYMENT`/`REFUND_MISALLOCATION` to Payments Ops, `FEE_VARIANCE`/`PERIOD_CUTOFF` to Finance Ops, `TAX_VARIANCE` to Tax & Compliance, `ORPHAN_CHARGEBACK` to Disputes & Risk, `FX_VARIANCE` to Treasury, `HIGH_VALUE_MATCH_REVIEW` to Finance Controller (the project's own namesake role), and `UNIDENTIFIED_CREDIT`/`UNRECONCILED`/`AGENT_INCOMPLETE`/`UNEXPLAINED_VARIANCE` staying with Reconciliation Ops as the genuine generalist catch-all for open-ended investigation. Wired through both producers - `engine/exceptions.py`'s `_exc()` and `engine/l3_tools.py`'s `raise_exception` plus its `HIGH_VALUE_MATCH_REVIEW` companion. `freeze_2000` now shows 6 distinct owners across its 139 exceptions, not 1. Baselines (`engine/baselines.py`) deliberately left untouched - they exist to validate the harness, not to model realism, same exemption as their `aging_days`.

## Phase 7: submission polish

No new engine code, per the schedule's own rule — never cut the generator's ground truth, the eval harness, or the exception ledger; everything else is presentation.

- **`README.md`** restructured to the brief's own mandated order: one-line description, results above the fold, reproduce in 3 commands (`git clone && pip install -e . && make demo`), an architecture summary + link here, then limitations. The per-phase deep-dive narrative that used to live in README (fee model, defect list, reference-run stats, baseline table) moved into this file's Phase 1/2 sections instead, so there's exactly one place each fact lives rather than two copies drifting apart — fixed one stale cross-reference (`ARCHITECTURE.md` linking to a README anchor that no longer existed) while doing it.
- **`DEMO_SCRIPT.md`**: the 5-minute demo script from the brief's own timing breakdown, filled in with this repo's actual committed numbers and commands rather than placeholders — including an honest staging note for the "click an exception → agent trace" beat, since `run_2000`'s own exception ledger has no agent traces to click (by design — see Phase 5/6 above) and the real traces live in `benchmarks/sample_traces/` instead.

Full detail in [`KOSH_BUILD_PROMPT.md`](KOSH_BUILD_PROMPT.md).
