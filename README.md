# Kosh — AI Finance Controller

Three-way payment settlement reconciliation for an Indian online merchant: **Orders ↔ PG ledger ↔ Bank statement**, tied out to the paisa, plus a forward cash position.

Built for the Razorpay AI Buildathon 2026, Track 04.

> **Status: Phase 6 of 7.** This repo ships the synthetic data generator with ground truth, the eval harness, L0–L2 matching, L3 (a provider-agnostic LLM agent) + L4 (the exception ledger), and now the forward cash position + a 4-panel dashboard, with the benchmark frozen at 3 scales. See [`KOSH_BUILD_PROMPT.md`](KOSH_BUILD_PROMPT.md) for the full 7-phase build plan and [`ARCHITECTURE.md`](ARCHITECTURE.md) for the target system design.

## Results — benchmark freeze, 3 scales

The complete pipeline (`engine=full`, deterministic-only), run at `seed=42` on all three sizes and committed as-is:

| Records | Auto-match | Precision / Recall | False-match | Exceptions | Wall clock |
|---|---|---|---|---|---|
| 500 | 97.83% | 100.00% / 100.00% | **0.00%** | 47 | ~2ms |
| 2,000 | 97.85% | 100.00% / 100.00% | **0.00%** | 132 | ~7ms |
| 10,000 | 97.83% | 100.00% / 100.00% | **0.00%** | 521 | ~31ms |

Consistent across a 20x size range, wall clock scales linearly with no quadratic blowup. See [`benchmarks/freeze_500.json`](benchmarks/freeze_500.json), [`freeze_2000.json`](benchmarks/freeze_2000.json), [`freeze_10000.json`](benchmarks/freeze_10000.json) (`.html` alongside each for the full dashboard).

## Results — Phase 5 (L0 + L1 + L2 + L4, no LLM), `data/fixtures/run_2000`

| Metric | Value |
|---|---|
| Auto-match rate | **97.85%** |
| Precision / Recall | **100.00% / 100.00%** |
| **False-match rate** | **0.00%** |
| Wall clock (2000 records) | **~7ms** |
| Exceptions raised | **132**, all 12 defect types covered, 13/14 recall on `PERIOD_CUTOFF` (see Limitations) |
| LLM calls on this fixture | **0** — the deterministic classifier resolves every case |

See [`benchmarks/phase5.json`](benchmarks/phase5.json) / [`.html`](benchmarks/phase5.html).

## Results — L3 agent, real run against a live LLM

`run_2000`'s residual is empty (see below), so L3's real capability is proven on a small hand-built synthetic exercise set instead — 5 records engineered to need a different kind of investigation each, run for real against `nvidia/nemotron-3-ultra-550b-a55b` via NVIDIA NIM (the brief specs Anthropic; no Anthropic key was available — see Limitations):

| Record | Outcome |
|---|---|
| `pay_unexplained` (delta fits no known hypothesis) | Correctly raised `UNEXPLAINED_VARIANCE` |
| `pay_ambiguous` (two identical-amount settlement candidates) | Correctly refused to guess, raised an exception |
| `btxn_batch` (a genuine 2-settlement batch) | **Correctly chained `find_candidates` → `solve_subset` → `propose_match`**, both settlements linked, high-value companion exception auto-attached |
| `pay_high_value`, `pay_weak_evidence` | `AGENT_INCOMPLETE` — thorough, correct investigation that ran out of its 8-turn budget before closing the loop |

**Zero false matches across the run.** That's the headline, and it held on a live, non-scripted model: a first pass surfaced a genuine bug (`propose_match` mislabeled a payment's own settlement credit as a chargeback when the model bundled a 3-record chain into one call — see commit `81cc88c`), which was fixed and verified with regression tests *before* the reference run below. Committed as [`benchmarks/phase5_synthetic.json`](benchmarks/phase5_synthetic.json) with full traces in [`benchmarks/sample_traces/`](benchmarks/sample_traces/).

## Why we generate our own data

The judging bar for this track: *"Throughput plus measured accuracy plus an honest exception list. One cherry-picked match proves nothing."*

You cannot measure precision, recall, or false-match rate against real production data, because you don't have ground truth for real data — that's the whole reconciliation problem. So Kosh generates its own three-way dataset with **injected, labelled defects**, and every claim the reconciliation engine (Phases 3–5) will later make gets checked against a machine-readable `ground_truth.json`.

## What's in Phase 1

```bash
python -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/
```

produces, deterministically:

- `orders.csv` — merchant ERP/invoice export
- `pg_payments.csv` — payment gateway transactions
- `pg_settlements.csv` — payment gateway settlement batches
- `bank_statement.csv` — bank credits/debits with realistically ugly free-text narration
- `ground_truth.json` — the true order↔payment↔settlement↔bank_txn link graph, plus every injected defect labelled with its expected exception category and whether a deterministic engine should be able to resolve it
- `manifest.json` — row counts, defect counts by type, GMV, and a sha256 of every emitted file

Same `--seed` → byte-identical output, every time.

### Reference run (`data/fixtures/run_2000`, seed 42, 3 months)

| Metric | Value |
|---|---|
| Orders | 2,000 |
| PG payments (incl. injected duplicates) | 2,020 |
| Settlements | 305 |
| Bank statement rows | 351 |
| GMV | ₹14,779,996.39 |
| Injected defects | 188 (~9.4% of orders), all 12 types present |

A small companion fixture, `data/fixtures/sample_200` (seed 7, 1 month), is committed to the repo so you can inspect real output without running the generator.

### The fee model

Realistic Indian PG economics — this is what makes the "interesting" failures concentrate where they should:

| Method | MDR |
|---|---|
| UPI | 0.00% (zero-MDR P2M — real regulatory position) |
| RuPay debit | 0.00% |
| Card (domestic) | 2.00% |
| Netbanking | 1.90% |
| Wallet | 2.00% |
| Card (international) | 3.00% |

Because UPI and RuPay carry zero MDR, most of that volume reconciles trivially — `net == gross`, no fee/GST math to get wrong. That's intentional, not a shortcut: it means the interesting defects (fee-tier errors, GST variance, FX drift) concentrate in card and international volume, which is exactly what a real merchant's exception queue looks like.

GST is 18% of the fee, and rounding is **integer half-up, applied at each step** (fee first, then GST on the rounded fee) — not once at the end. That ordering produces genuine ±1 paisa drift, which is one of the 12 injected defect types and also just how real gateways compute it.

**All money is integer paise. There are no floats anywhere in the money path** — enforced by an AST-level lint test (`tests/test_no_floats.py`) that fails the build on a stray `/`, `round()`, or float literal in the fee/settlement arithmetic.

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

## What's in Phase 2: the eval harness

Before any matching logic exists, the harness that will grade it does — this is the whole point of leading with data generation. `engine/contract.py` defines the interface every future layer (L0–L3) and every baseline "engine" must emit: a list of asserted `Match`es (link_type + left/right IDs + confidence + evidence) and a list of `ReconException`s (category, severity, ₹ at risk, recommended action). `eval/metrics.py` scores any such output against `ground_truth.json`.

Two baseline engines prove the harness itself is honest before it's asked to grade anything real:

```bash
python -m eval.report --fixtures data/fixtures/run_2000 --engine null   --label null_run2000
python -m eval.report --fixtures data/fixtures/run_2000 --engine oracle --label oracle_run2000
```

| Baseline | What it does | auto-match | precision / recall | false-match rate | exceptions |
|---|---|---|---|---|---|
| `null` | asserts nothing | 0.00% | 0.00% / 0.00% | 0.00% | 100% of records |
| `oracle` | reads `ground_truth.json` directly | 97.85%* | 100.00% / 100.00% | **0.00%** | 133 (exactly the unresolvable defects) |

\* Not 100%: payments hit by `missing_settlement` or `duplicate_payment` have no true settlement link to match — they correctly land on the exception ledger instead, which is what auto-match rate is supposed to show.

**`false_match_rate` is the headline metric** — in finance a wrong match is worse than no match, because it silently corrupts the books where an unmatched item merely sits in a queue. Both baselines are frozen as a regression guard in `tests/baselines/*.json`: if `eval/metrics.py`'s scoring logic drifts, or a baseline's behavior drifts, `pytest` catches it immediately.

`benchmarks/run_null_run2000.{json,html}` and `run_oracle_run2000.{json,html}` are the committed example reports — open the `.html` ones directly in a browser.

## Reproduce

The fastest path — clone, install, one command, open the dashboard:

```bash
git clone <this-repo> && cd kosh
pip install -e .
make demo   # generates run_2000, runs the full pipeline, writes benchmarks/run_demo.html
```

Everything else:

```bash
pip install -e ".[dev]"
python -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/
python -m data.generator.trace --fixtures data/fixtures/run_2000 --pick-clean   # hand-verify one full chain
python -m eval.report --fixtures data/fixtures/run_2000 --engine full --label phase5
python -m engine.l2_subset --profile --trials 2000 --seed 42
export NIM_API_KEY=...   # only needed to re-run the live L3 exercise for real
python -m engine.l3_agent --profile --backend nim --model nvidia/nemotron-3-ultra-550b-a55b
pytest   # pip install -e ".[dev,llm]" first, or the two LLM-client test files skip gracefully
```

`make gen`, `make sample`, `make test`, `make trace`, `make eval-null`, `make eval-oracle`, `make eval-l0l1`, `make eval-l0l1l2`, `make eval-full`, `make l2-profile`, `make l3-profile`, and `make demo` wrap the same commands.

## Repo layout

```
data/generator/   synthetic dataset generator (Phase 1)
engine/           EngineOutput contract, null/oracle baselines, L0+L1+L2 (Phases 2-4), L3 agent + llm/ adapter + L4 ledger (Phase 5)
eval/             eval harness: metrics against ground truth, benchmark reports + 4-panel dashboard (Phases 2, 6)
cash/             forward cash position: SLA forecast, stuck cash, book-vs-reconciled identity (Phase 6)
api/              FastAPI layer (not yet built)
tests/            pytest suite, incl. tests/baselines/ regression fixtures
benchmarks/       committed reports: null/oracle baselines, phase3-5, phase4_solver_perf, phase5_synthetic, freeze_{500,2000,10000}, sample_traces/
```

## What's in Phase 3: L0 + L1

`engine/l0_deterministic.py` cascades three exact-key joins: `orders.order_id ↔ pg_payments.order_id`, `pg_payments.settlement_id ↔ pg_settlements.settlement_id`, and `pg_settlements.utr ↔` a UTR extracted from `bank_statement.narration` (via `engine/normalize.py`). UTR extraction handles a full 16-character match, a truncated prefix (≥12 chars, only if it uniquely identifies one settlement), and refuses to guess otherwise. `engine/l1_tolerance.py` catches whatever L0 couldn't place — chiefly a bank credit whose UTR was rekeyed with a transposed digit — by requiring amount within ±₹3, date within ±3 days, *and* narration that reads as a genuine settlement credit, all three, and only when exactly one settlement satisfies them.

`engine/fees.py` re-exports the generator's fee model unchanged (so a fee bug is a generator-test failure, never a fake match) and adds `explain_variance`, which decomposes an observed-vs-expected paise delta into a known cause — rounding, a wrong fee tier, a wrong GST rate, a known refund — or honestly returns `UNEXPLAINED`. This is the function Phase 5's exception ledger will lean on.

Two ambiguity guards are load-bearing, not incidental: an L0 UTR-prefix match that fits more than one settlement is refused, and an L1 candidate set with more than one plausible settlement is refused — both escalate rather than pick. `tests/test_l0_l1.py` exercises both directly (constructed cases, since the reference fixture doesn't happen to produce a genuine collision) alongside the Phase 3 checkpoint on `run_2000`.

## What's in Phase 4: L2

`engine/l2_subset.py` solves the "one bank credit is a batch" problem the brief describes: given a still-unresolved credit and a pool of settlements within a same-day/T+1 date window, find the subset that sums to it. The two approaches the spec suggests both have a scaling trap in this domain — meet-in-the-middle costs O(2^(n/2)) in the candidate count (dangerous at n=40), and a naive bitset DP, while cheap for a one-off feasibility check, costs O(bit-length) *per node* if reused during subset recovery — and bit-length here means paise, so it scales with money magnitude, not candidate count. Profiling this directly is what caught it: an early version blew the 250ms budget on nothing more exotic than 40 candidates around ₹50,000 each. The shipped solver instead runs a depth-first search pruned by a cheap O(1) sum-bound at every node, so per-node cost depends only on n, and a node-count-based time check gives a real, predictable worst case: **p50 ~2ms, p99 ~50ms, max ~101ms** against 2,000 synthetic instances at n up to 40 — see [`benchmarks/phase4_solver_perf.json`](benchmarks/phase4_solver_perf.json).

The ambiguity guard here is the same principle as L0/L1's, applied to subsets rather than single candidates: if more than one distinct subset satisfies the target, the solver returns `AMBIGUOUS` with every alternative it found (capped at 2, since "more than one exists" is all that matters) rather than picking one. A pool over `max_terms` (40) or a search that hits its time budget both escalate the same way `NONE` does — L2 simply doesn't match that credit, leaving it for L3/L4.

**Honestly: L2 contributes zero matches on `run_2000`.** L0+L1 already achieve 100% recall on every tie-outable `settlement_bank_txn` link in this fixture — a split settlement is resolved by both bank credits sharing the same UTR (L0), and a mangled UTR is resolved by amount+date+narration tolerance (L1) — so nothing genuinely batched-and-unrecoverable reaches L2 here. This is verified, not assumed: `tests/test_l2_subset.py` covers the solver and its pipeline wrapper (a genuine multi-settlement batch, the ambiguity guard, the already-matched exclusion, the date-window prune) against constructed cases, and `tests/test_l2_subset_perf.py` is the p99-under-250ms regression guard, run against synthetic instances rather than this dataset.

## What's in Phase 5: L3 agent + L4 exception ledger

**A real gap fixed first.** Designing L4's classifier exposed that `bank_statement.csv`'s chargeback narration carried no reference back to its originating payment at all — legitimate and orphan chargebacks were textually identical, so no engine could ever have told them apart from the public data as it stood. Fixed by embedding a *derivable* reference in legitimate-chargeback narration (`data/generator/ids.py:derive_dispute_ref` — a pure transform of the payment_id, the same "engine can independently recompute this" pattern a settlement's UTR already uses), exposed as a 4th ground-truth link (`chargeback_to_payment`) and a 4th L0 matcher. Verified purely additive: `run_2000`'s auto-match and false-match rates are unchanged; the new link type scores 100% recall on its own 8 legitimate chargebacks.

**L4** (`engine/exceptions.py::classify_deterministic`) covers every defect category with an honest, generalizable rule — `explain_variance` for fee/GST/refund causes, a settlement-vs-latest-capture gap for `PERIOD_CUTOFF` (empirically: normal is 1-4 days, genuine cutoffs are 3-31 days; the threshold is set to catch 13/14 with **zero** false positives rather than 14/14 with ~20 false alarms — the one boundary case is an honest miss, not a guess), and structural checks for the rest. On `run_2000`: **0 LLM calls, 132 exceptions, every non-resolvable defect type covered.**

**L3** (`engine/l3_agent.py`, `engine/l3_tools.py`) is provider-agnostic — the brief specs Anthropic; no Anthropic key was available, an NVIDIA NIM key was, and NIM doesn't serve Claude. `engine/llm/` defines a minimal `LLMClient` interface; `AnthropicClient` is spec-complete and unit-tested against a mocked SDK (ready for a real key), `NimClient` is what actually ran for real. Every hard constraint from the brief is enforced structurally in the tool layer, not just prompted — see the Results section above for what a real, non-scripted run against `nvidia/nemotron-3-ultra-550b-a55b` actually did, including a genuine false-match bug it surfaced and that got fixed with regression tests before the reference run.

## What's in Phase 6: cash position + dashboard + benchmark freeze

**`cash/forecast.py`** is the other half of "run the books *and* the cash position." Settlement SLA reuses `data.generator.calendar.settlement_date` unchanged (same "one source of truth" pattern as `engine/fees.py`). The brief's hardest requirement here — book cash (accrual, gross on every captured payment) vs. reconciled cash (actual net bank movement) must tie **to the paisa** — was verified the same way every other identity in this project was: computed, checked against `run_2000`, and iterated until the residual hit exactly zero. Two real formula bugs surfaced doing that (not fixture noise): `unsettled_gross_paise` was summed on `net_paise` instead of `gross_paise` (an unsettled payment hasn't been through the fee-deduction pipeline at all, so its full gross belongs in the gap), and the fee/GST term was reconstructed as `fee_paise + gst_paise`, which silently drops `rounding_drift` (applied directly to `net_paise`, never touching the fee/GST fields) — replaced with `gross - net` directly, the true source of truth regardless of which defect caused the gap. Residual is now exactly 0 on both committed fixtures.

**Dashboard**: rather than a separate Next.js app, the brief's own fallback rule ("a working CLI + report.html beats a broken dashboard, judges grade accuracy not CSS") is taken literally — `eval/report.py`'s static HTML report now covers all 4 panels: headline strip (incl. **₹ reconciled**, previously missing), layer waterfall, a sortable, click-to-expand exception queue (evidence chain, affected records, and a link to the agent trace when one exists — checked against `benchmarks/sample_traces/`), and the cash position (14-day inflow chart + the full book-vs-reconciled breakdown, rendered as named line items that sum exactly). No build step, no framework, verified interactively in the browser (bar heights, click-to-expand, click-to-sort all confirmed via direct DOM inspection), not just by reading the HTML source.

**Benchmark freeze**: `data/fixtures/run_{500,2000,10000}` (seed 42) through the complete pipeline, committed as `benchmarks/freeze_{500,2000,10000}.{json,html}` — see the results table above the fold. **Fresh-clone checkpoint verified for real**, not assumed: cloned into an isolated temp directory, `pip install -e .` in a brand-new venv, `make demo` ran clean. That run also caught a real gap — `pytest` crashed at collection (not just failed) because `test_anthropic_client.py`/`test_nim_client.py` import `anthropic`/`openai` unconditionally, and those live in the optional `llm` extra, not the base install. Fixed with `pytest.importorskip` so a bare install gets 2 graceful skips instead of an interrupted run.

## Limitations (Phase 1–6)

- L2 has no real residual to solve on the reference fixture (see above) — its correctness and timing are verified by direct unit/perf tests, not by this fixture's own numbers. The same is true of L3: `run_2000`'s residual for it is empty, so its real capability is proven on the synthetic exercise set instead.
- `PERIOD_CUTOFF` recall is 13/14 (92.9%) on `run_2000` — one boundary case (a 3-day settlement gap) is genuinely indistinguishable from ~20 normal same-gap settlements using the available signal; reported as a miss rather than guessed at the cost of false positives.
- `propose_match`'s rationale-citation check (constraint 5) is a best-effort structural check (does the text contain a known record id), not a semantic verification that the citation actually supports the claim.
- Two of the five live L3 exercise records ended in `AGENT_INCOMPLETE` rather than a clean decision — both were the model doing thorough, correct investigation that simply didn't fit inside the 8-turn budget. This is constraint 6's infrastructure-level fallback working as designed, not a false positive, but it's an honest sign the turn budget is tight for a genuinely uncertain case.
- `auto_match_rate` and `hands_off_rate` are defined identically for now (see `eval/metrics.py` module docstring) — a documented simplification that holds until a layer can leave a record neither matched nor exceptioned.
- `false_match_rate` is computed against the engine's own asserted links (wrong / total asserted), not against total records — deliberate, so it can't be gamed by asserting very few links, but it means it must always be read next to auto-match rate, never alone.
- The reference fixture's own truncation defect happens to either leave the UTR fully intact or remove it entirely (its longest template prefix already exceeds the 35-char cutoff) — L0's partial-prefix-match branch is exercised by unit test, not by `run_2000` itself.
- Volume seasonality, ticket-size distributions, and defect rates are hand-tuned to look like a mid-size D2C merchant; they are not calibrated against any real portfolio.
- The bank calendar covers 2025–2026 national holidays only, not state-specific ones.
- `cash/forecast.py`'s "as of" date is inferred from the dataset's own latest capture date, not passed in — fine for a fixed historical fixture, but a real deployment would pass the actual current date explicitly.
- No FastAPI layer (`api/`) or a live/interactive dashboard — the static HTML report is the deliverable, per the brief's own fallback preference.
