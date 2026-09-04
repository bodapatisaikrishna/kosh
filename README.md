# Kosh — AI Finance Controller

[![CI](https://github.com/bodapatisaikrishna/kosh/actions/workflows/ci.yml/badge.svg)](https://github.com/bodapatisaikrishna/kosh/actions/workflows/ci.yml)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-257%20passing-brightgreen)](tests/)
[![False-match rate](https://img.shields.io/badge/false--match%20rate-0.00%25-brightgreen)](benchmarks/)

**Three-way payment settlement reconciliation for an Indian online merchant** — Orders ↔ PG ledger ↔ Bank statement, tied out to the paisa, plus a forward cash position.

Built for the **Razorpay AI Buildathon 2026, Track 04** — *"Run the books and the cash position."*

One bank credit is rarely one payment. It's a *batch*: the sum of several settlements, minus refunds, minus chargebacks, plus or minus adjustments — and the reference number that would tie it back is often mangled, truncated, or absent entirely. Today a human solves that in a spreadsheet. Kosh solves it in **8 milliseconds for 2,000 records**, and — the part that actually matters — it **refuses to guess** when the evidence is ambiguous.

| | |
|---|---|
| **Auto-match rate** | 97.74% @ 2,000 records (97.83% @ 10,000) |
| **False-match rate** | **0.00%** — at every scale, across 6 seeds, under 7 adversarial attacks |
| **Money found** | **₹10,475.40** in gateway fee / tax / FX overcharges, across 2,000 transactions |
| **Throughput** | 219,659 records/sec deterministic (8.46ms for 2,000) |
| **LLM cost** | $0.056 per 1,000 records — only 0.3% of records ever reach the model, at both 2k and 10k scale |
| **Verification** | 257 tests, 89% coverage, reproduced byte-for-byte from a clean clone |

> The track's own bar: *"Throughput plus measured accuracy plus an honest exception list. One cherry-picked match proves nothing."*
>
> Every number on this page is measured against a machine-readable `ground_truth.json` with injected, labelled defects — never asserted, never hand-picked. The scale requirement is 50+ records; Kosh is frozen at 500 / 2,000 / 10,000.

**Contents** — [Results](#results) · [Why you can trust these numbers](#why-you-can-trust-these-numbers) · [Reproduce](#reproduce) · [Architecture](#architecture) · [Live API + dashboard](#live-api--interactive-dashboard-post-freeze-stretch-goal) · [Why we generate our own data](#why-we-generate-our-own-data) · [Limitations](#limitations)

---

## Results

**Frozen benchmark, complete pipeline, 3 scales** ([`benchmarks/freeze_*.json`](benchmarks/)):

| Records | Auto-match | Precision / Recall | **False-match** | Exceptions | Wall clock |
|---|---|---|---|---|---|
| 500 | 97.61% | 100.00% / 99.91% | **0.00%** | 48 | ~3ms |
| 2,000 | 97.74% | 100.00% / 99.95% | **0.00%** | 139 | ~8ms |
| 10,000 | 97.83% | 100.00% / 100.00% | **0.00%** | 550 | ~40ms |

**False-match rate is the headline metric, not auto-match rate.** In finance a wrong match is worse than no match — it silently corrupts the books, where an unmatched item merely sits in a queue for review. It reads **0.00%** at every scale tested, including a real, non-scripted LLM run — checked directly against ground truth, not asserted.

**₹10,475.40 in fee leakage** — the industry-standard reconciliation metric, and the number a finance team actually cares about. That's what the merchant was overcharged in gateway fees, tax on those fees, and FX across `run_2000`'s 2,000 transactions (42 records; `FEE_VARIANCE`/`TAX_VARIANCE`/`FX_VARIANCE` only — timing, duplication, and attribution problems are deliberately excluded, since folding them in would inflate the number into meaninglessness). Reported as a **lower bound**, always: a compound error coerced to `UNEXPLAINED_VARIANCE` contains real leakage this number cannot isolate to a single fee leg.

**Every layer earns its place** — measured, not asserted:

| Layer | 500 | 2,000 | 10,000 | What only it can do |
|---|---|---|---|---|
| L0 exact-key | 97.76% | 99.33% | 99.82% | UTR / FK joins |
| L1 tolerance | 0.78% | 0.26% | 0.06% | UTR rekeyed with a transposed digit |
| L2 subset-sum | **1.47%** | **0.41%** | **0.12%** | consolidated payouts — one credit, 2–4 settlements, no per-settlement UTR |
| L3 agent | 1 record | 6 records | 29 records | variances no deterministic rule can decompose |

L3 saw **6 of 1,858 records (0.32%)** on `run_2000` — and **29 of 9,317 (0.31%)** on `run_10000`. That ratio holding flat across a 20× scale increase is the deterministic-first thesis quantified: the residual grows linearly, not explosively, so LLM cost stays a rounding error at any scale. The other 99.7% cost zero tokens.

**Ablation — what each layer, and an all-LLM alternative, actually buys you:**

| Engine mode | Fixture | Auto-match | False-match | Precision / Recall | Cost |
|---|---|---|---|---|---|
| Null (matches nothing) | `run_2000` | 0.00% | 0.00% | 0.00% / 0.00% | $0 |
| L0 + L1 | `run_2000` | 92.84% | **0.00%** | 100.00% / 99.54% | $0 |
| L0 + L1 + L2 | `run_2000` | 97.74% | **0.00%** | 100.00% / 99.95% | $0 |
| Full (+ L3 + L4) | `run_2000` | 97.74% | **0.00%** | 100.00% / 99.95% | $0 (deterministic CLI path) |
| **All-LLM** (L3 only — L0–L2 *and* L4 bypassed) | `sample_200`\* | 81.52% | **0.00%** | 100.00% / 77.36% | $6.58 → **$35.76/1000 records** |

\* 348 non-order records (payments + settlements + bank), not the 1,858-record `run_2000` — routing everything through a live LLM at 2,000-record scale costs materially more for the same architectural point. Real run: `nvidia/nemotron-3-ultra-550b-a55b` via NVIDIA NIM, 1,207 LLM calls, 348/348 records, zero crashes. Source: [`benchmarks/ablation_llm_only.json`](benchmarks/ablation_llm_only.json).

**The all-LLM row is the actual evidence for the architecture, not a knock against the model.** False-match holds at 0.00% even with every deterministic layer disabled — the "refuse rather than guess" discipline survives. What drops is *recall*: more genuinely ambiguous records get correctly refused rather than confidently matched, at **30–50× the cost** of the deterministic layers doing the same job for the 99.68% of records that never needed judgment. Deterministic-first isn't a shortcut around the LLM; it's what reserves LLM judgment for the cases that actually need it.

**Per-defect-class scoring on `run_2000` — all 14 injected types, zero misses, zero false alarms:**

| Correctly flagged as exceptions | | Correctly resolved *silently* (must NOT become exceptions) | |
|---|---|---|---|
| `missing_settlement` | 20/20 | `rounding_drift` | 25/25 |
| `duplicate_payment` | 20/20 | `utr_mangled` | 20/20 |
| `fee_mismatch_wrong_tier` | 19/19 | `settlement_split` | 11/11 |
| `gst_variance` | 15/15 | `consolidated_payout` | 8/8 |
| `period_cutoff` | 14/14 | | |
| `orphan_chargeback` | 14/14 | | |
| `unidentified_credit` | 14/14 | | |
| `refund_misallocation` | 9/9 | | |
| `fx_variance` | 8/8 | | |
| `compound_fee_tax_error` | 3–6/6\* | | |

The right-hand column is the half most systems get wrong: four defect types exist specifically to punish an engine that flags everything it doesn't instantly recognise. Precision on *not* raising a false alarm is scored as hard as recall on real defects. Every one of these 13 classes is exact; the 14th is the live-LLM one, below.

\* `compound_fee_tax_error` is L3's live residual, not deterministic, so it varies run to run with how thoroughly the model investigates. The best evidence is the larger sample: run live against `run_10000`'s **29-record residual**, L3 scored **25/29 (86%)** — see [the full-scale L3 run](#l3-at-full-scale-29-records) below. The four `run_2000` runs (6, 4, 5, 3 out of 6) are the same behaviour on a sample too small to draw a rate from. See [Limitations](#limitations).

**Exception aging** against the industry 48-hour SLA for open reconciliation breaks: median 35 days, max 89, 129 of 139 breaching. **This is not a live queue** — `run_2000` is a fixed, historical 3-month fixture scored against its own end date, so aging this large is the expected result of scoring a static snapshot, not a finding about operational neglect. Stated here rather than quietly omitted, because the number looks alarming and isn't.

### L3, run for real

6 records that L0–L2 and L4's deterministic classifier genuinely could not resolve, sent live to `nvidia/nemotron-3-ultra-550b-a55b` via NVIDIA NIM ([`benchmarks/phase5_live_residual.json`](benchmarks/phase5_live_residual.json)). *The brief specifies Anthropic's Claude; no key was available — a documented deviation, not a silent one.*

| Record | Outcome |
|---|---|
| `pay_dGxUjmPIxeeXo4` | Correctly **matched** to its settlement — independently checked against `ground_truth.json`. Its 88-paise fee/GST anomaly judged immaterial, not separately flagged. |
| `pay_OyvjU0Hc7g7Bi2` | Correctly raised `UNEXPLAINED_VARIANCE`, ₹2,286.93 — exact, the largest of the six |
| `pay_RMejvzSwrh9QXa` | Correctly raised `UNEXPLAINED_VARIANCE`, ₹1.83 — exact to the true net delta |
| `pay_Yw6hEZsEyvZMNn` | Correctly raised `UNEXPLAINED_VARIANCE`, ₹1.42 — exact |
| `pay_ymzQx3u8WEhd7G` | Correctly raised `UNEXPLAINED_VARIANCE`, ₹41.19 — exact |
| `pay_3egKQ6BCralBAI` | Raised `TAX_VARIANCE`, ₹0.90 — exact amount, single-cause label: the model checked only the GST leg this run, so the multi-leg coercion correctly did not fire on one data point |

Zero `AGENT_INCOMPLETE`, zero false matches, zero invented categories — verified by hand against ground truth for every asserted link, category, and amount, not read off the summary. 51 real LLM calls, 487s wall clock, **$0.104 total → $0.056 per 1,000 records** (the brief's target is <$0.50/1000), computed from real token counts against NIM's published rate — not estimated. Full traces: [`benchmarks/sample_traces_live/`](benchmarks/sample_traces_live/).

**The structural fix behind it**: L3's tool layer recomputes the category to `UNEXPLAINED_VARIANCE` — and the amount to the true net delta, not one leg's — whenever the model's own tool-call history shows 2+ comparisons it couldn't decompose. The prompt asks; the tool layer *enforces*. It fires when the evidence supports it and correctly refuses on weaker evidence, rather than papering over real run-to-run variance by guessing.

### L3 at full scale (29 records)

The `run_2000` residual is only 6 records — too few to claim a rate from. So the agent was also run live against **`run_10000`'s 29-record residual**: 245 LLM calls, 20 minutes, `nvidia/nemotron-3-ultra-550b-a55b`. Source: [`benchmarks/phase5_live_residual_10k.json`](benchmarks/phase5_live_residual_10k.json).

**Headline metrics did not move.** Auto-match 97.83%, false-match **0.00%**, precision 100%, recall 100%, 550 exceptions — byte-identical to the deterministic `freeze_10000` run. L3 touches 29 of 9,317 records; it cannot and did not shift the top-line numbers.

**On its own defect class, L3 scored 25/29 (86%)** — a real rate from a real sample, replacing four noisy readings off six records. The 4 it got wrong, verified by hand against `ground_truth.json`:

| Record | Outcome | Amount |
|---|---|---|
| `pay_4cXgekcH1NC0sO` | labelled `TAX_VARIANCE`, not `UNEXPLAINED_VARIANCE` | ₹1.30 — **exact** |
| `pay_l23vdlUo60FAXP` | labelled `FEE_VARIANCE` | ₹205.79 — **exact** |
| `pay_z8mbHMXa8wIMih` | labelled `TAX_VARIANCE` | ₹0.33 — **exact** |
| `pay_wkjO7N4t4iTfU1` | `AGENT_INCOMPLETE` — exhausted its 12-turn budget | fallback hint, not a computed claim |

Three of the four are **label-only misses with the money exactly right** — the model found the real variance, then named one cause instead of "multiple causes, undecomposable," because the multi-leg coercion correctly declined to fire on insufficient evidence.

**Two findings the 6-record sample could never have surfaced:**

1. **A turn-budget exhaustion at 29 records** (1 of 29, ~3%). Every `run_2000` run reported zero `AGENT_INCOMPLETE`, which made the 12-turn budget look sufficient. At scale it isn't, always. The fallback did its job — the record is on the ledger, flagged for review, with the reason attached — but its stated amount is a record-level hint, not a measured variance, and reads far larger than the true ₹1.82. Trace: [`sample_traces_live_10k/agent_incomplete_turn_budget.json`](benchmarks/sample_traces_live_10k/agent_incomplete_turn_budget.json).
2. **The deterministic fallback outscores the LLM on this class** — 29/29 vs 25/29. Not a paradox: with no client, every residual record is blanket-labelled `UNEXPLAINED_VARIANCE`, which for `compound_fee_tax_error` is definitionally correct every time. The LLM attempts a specific cause and is wrong 4 times out of 29. **On this one class, the cheap fallback wins.** L3's value is on residuals that are genuinely decomposable — not on a class defined by being undecomposable.

Reported because it's what the run produced, not because it flatters the architecture.

---

## Why you can trust these numbers

A 0.00% false-match rate is exactly the kind of claim that should invite suspicion. So it was attacked, not just measured:

| Evidence | What it rules out | Source |
|---|---|---|
| **7 adversarial attacks**, hand-built to force a false match at each layer — transposed-digit UTRs, coincidental subset sums, a refund that makes two settlements collide, one UTR on two bank rows | "It only works on friendly data" — **0 of 7 produced a false match**; all `REFUSED` or `CORRECT`. Attack `f` found a **real double-claim bug**, fixed, retested | [`benchmarks/adversarial.json`](benchmarks/adversarial.json), `make adversarial` |
| **L3 run live at full scale** — 29-record residual from `run_10000`, 245 real LLM calls, fresh cache | "The agent claims rest on 6 records" — a real rate (25/29), and it surfaced a turn-budget exhaustion plus the fallback-beats-LLM finding that 6 records hid | [`phase5_live_residual_10k.json`](benchmarks/phase5_live_residual_10k.json) |
| **6 independent seeds**, fresh 2,000-record fixture each | "It's a `seed=42` artifact" — mean 97.71%, stddev 0.36pp, **0.00% false-match on every seed** | [`benchmarks/multiseed/summary.json`](benchmarks/multiseed/summary.json), `make multiseed` |
| **Mutation-tested harness** — inject 10 deliberately wrong links, it reports 0.24%; drop half the true matches, recall halves | "The scorer is vacuous / always says zero" — it demonstrably fails when the engine is wrong | `tests/test_eval_baselines.py` |
| **Null + oracle baselines**, frozen as regression fixtures | Scorer drift going unnoticed | `tests/baselines/` |
| **Determinism test** — two runs, byte-identical output, no duplicate links or ledger entries | Hidden nondeterminism | `make verify-deterministic` |
| **13 malformed-input cases** — missing column, duplicate header, non-UTF8 bytes, duplicate primary key, row overflow | Silent mis-reconciliation of a broken bank export; every case fails loudly with file, row, and field named | `tests/test_malformed_input.py` |
| **257 tests, 89% coverage, CI on Python 3.11 + 3.12**, integer-paise AST lint, pinned lockfile | "It passes on the author's machine" | `.github/workflows/ci.yml`, `pytest` |

Every frozen benchmark reproduces **byte-for-byte from a clean clone against the committed lockfile** — accuracy, exceptions, fee leakage, and aging all verified identical, not assumed.

**And the failures are on the record too.** [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`RESULTS.md`](RESULTS.md) document every bug found and how it was caught — including two genuine false-match bugs (one surfaced by a live model mid-run), a settlement double-claim whose *first* fix broke a working feature and was reverted, a cost field that silently reported $0 while a real account was being billed, a 10-hour live-run hang, and a healthy process killed on stale evidence during that investigation. Nothing here was smoothed over after the fact.

---

## Reproduce

```bash
git clone https://github.com/bodapatisaikrishna/kosh && cd kosh
pip install -e .
make demo
```

Opens `benchmarks/run_demo.html` — the full 4-panel dashboard (headline strip, layer waterfall, exception queue with evidence-chain and agent-trace drill-down, cash position) from a fresh 2,000-record run. Verified in an isolated clone on a clean venv, not assumed.

**Want to look before running anything?** [`benchmarks/freeze_2000.html`](benchmarks/freeze_2000.html) is the same dashboard, committed — identical engine, fixture, and numbers. Open it straight from the repo. (`run_demo.html` is regenerated output and deliberately not committed, same as `data/fixtures/` — everything reproducible from a seed stays out of git.)

```bash
make freeze              # regenerate all 3 scales + phase benchmarks
make multiseed           # the 6-seed sweep
make adversarial         # the 7 attacks
make verify-deterministic
make demo-cash           # cash forecast from an operator-chosen viewpoint
pytest                   # 257 tests
```

---

## Architecture

Deterministic first, LLM last — five layers, each seeing only what the one above couldn't resolve (shares from `run_2000`):

```
L0  Deterministic joins   (exact keys)         → 99.33% of matched links
L1  Tolerance matching    (±amount, ±date)     → 0.26%
L2  Combinatorial solver  (subset-sum)         → 0.41%
L3  LLM agent             (residual only)      → 6 records (0.32%) — 29 (0.31%) at 10k
L4  Exception ledger      (honest remainder)   → 139 exceptions, every category covered
```

**Each layer refuses rather than guesses when evidence is ambiguous — that's what holds false-match at zero.** L0 won't pick between two settlements sharing a UTR prefix. L1 won't pick the "closest" of two candidates in tolerance. L2 returns `AMBIGUOUS` rather than choosing one of several valid subsets. L3's tool layer structurally rejects any ID it didn't hand the model, any match under 0.85 confidence, and recomputes severity itself rather than trusting the model — the prompt asks, the tool layer enforces.

The generator is **deliberately adversarial to its own engine**: it injects consolidated payouts (one credit, several settlements, no per-settlement reference — solvable only by subset-sum) and compound fee+tax errors (two overlapping causes, so no single-cause hypothesis can decompose them — genuinely unexplained, real work for L3). That's what makes the layer shares above real measurements rather than a diagram.

**Stack**: Python 3.11+ and **zero runtime dependencies** — plain dataclasses, stdlib `csv`/`json` throughout (deliberately, so no float formatting ever gets near money), integer paise everywhere (enforced by an AST lint). `pytest` is a dev extra; the `anthropic`/`openai` SDKs are an optional extra behind a provider-agnostic `LLMClient` interface, needed only for a live L3 run. `pip install -e .` pulls in nothing at all.

```
data/generator/   synthetic dataset generator + injected, labelled defects
engine/           L0-L4: matching layers, the LLM adapter, the exception ledger
eval/             scoring against ground truth, the 4-panel HTML dashboard
cash/             forward cash position: SLA forecast, stuck cash, book-vs-reconciled
tests/            257 tests, incl. adversarial suite and frozen regression baselines
benchmarks/       committed reports at every phase + the 3-scale freeze + real agent traces
api/              post-freeze stretch goal: FastAPI layer over eval.report.run_eval
dashboard/        post-freeze stretch goal: Next.js + Recharts interactive dashboard
```

Full design rationale, and every bug with the reasoning that caught it, in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Live API + interactive dashboard (post-freeze stretch goal)

The brief's two optional *"if time allows"* items — a FastAPI layer and an interactive Next.js dashboard — built after the code freeze as a deliberate, dated addition. **Additive, not a replacement**: `make demo`'s static report stays the primary deliverable and needs nothing but Python.

```bash
pip install -e ".[api]"
make api          # FastAPI on :8000
make dashboard    # Next.js on :3000, second terminal
```

The dashboard does one thing the static report can't: a **live-triggered run** — pick engine, record count, seed, months, click *Run live*, watch a real `generate → reconcile → score` pass complete, then render the same four panels. It's a thin wrapper around the exact same `eval.report.run_eval` the CLI calls — no second implementation to drift.

- At `seed=42, records=2000, engine=full` the live run reproduces `run_2000` byte-for-byte, so its drill-down links resolve to the **actual committed live-model agent traces** — not placeholders.
- The costed live-LLM path is **never reachable** from the API — `ENGINE_ALLOWLIST` restricts every request to deterministic engines, the same invariant the CLI has always enforced, tested directly.

Local-only by design: CORS restricted to the dashboard's own origin, nothing deployed or publicly exposed.

---

## Why we generate our own data

You cannot measure precision, recall, or false-match rate against real production data, because you don't have ground truth for real data — **that is the reconciliation problem itself**. So Kosh generates its own three-way dataset with **injected, labelled defects** spanning 14 realistic failure modes (203 defects in `run_2000`): wrong MDR tier, GST variance, misallocated refunds, orphan chargebacks, FX drift, split settlements, consolidated payouts, and more — each labelled in `ground_truth.json` with its expected exception category and whether a deterministic engine should resolve it silently or flag it.

Realistic on purpose, not uniform: UPI/RuPay carry zero MDR (the real regulatory position), so most volume reconciles trivially and the interesting failures concentrate in card and international volume — same as a real merchant's exception queue.

```bash
python -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/
```

Same `--seed` → byte-identical output, every time. A small committed fixture, `data/fixtures/sample_200`, lets you inspect real output without running anything.

---

## Limitations

Written plainly, because an honest limitations list *is* the deliverable — a shorter list with something suppressed would be a worse submission.

- **Recall is 99.95% on `run_2000`, not 100%.** Two settlements net to exactly ₹0, so a bank credit genuinely cannot evidence whether they rode along in a consolidated payout — a zero-value term is degenerate in a subset-sum. Refusing costs 2 links; guessing would risk the false-match rate this project exists to protect.
- **L3's category for a compound fee+GST error can be a single-cause label** when the model's investigation surfaces only one unexplained leg. At full scale — `run_10000`'s 29-record residual — it scored **25/29 (86%)**; three of the four misses named one cause instead of "undecomposable" while getting the money *exactly* right, and one exhausted its 12-turn budget into `AGENT_INCOMPLETE`. On this specific class the deterministic fallback actually scores better (29/29), because blanket-labelling everything `UNEXPLAINED_VARIANCE` is definitionally correct for a defect defined by being undecomposable. L3 earns its place on residuals that *can* be decomposed, not this one. Sources: [`phase5_live_residual_10k.json`](benchmarks/phase5_live_residual_10k.json), [`sample_traces_live_10k/`](benchmarks/sample_traces_live_10k/).
- **The brief specifies Anthropic's Claude**; the real agent ran against NVIDIA NIM's Nemotron, because that's the key that was available. `AnthropicClient` is spec-complete and unit-tested against a mock, never run live. Documented, not hidden.
- **`PERIOD_CUTOFF`'s >4-day threshold is tuned to this fixture's distribution**, not a law — on a different merchant's cycle it needs re-derivation, and one boundary case at exactly 3 days is genuinely indistinguishable from a slow weekend.
- **Defect rates are tuned so all 14 types appear at N=2000**; at N=500 some fire once or twice, so per-class recall at that scale is a small-sample number.
- **`propose_match`'s rationale-citation check is structural, not semantic** — it verifies the text cites a known record ID, not that the citation actually supports the claim.
- **The cash forecast is viewpoint-dependent by nature.** `as_of` defaults to the dataset's own latest capture date — the least informative viewpoint, since almost nothing is still in flight by then (2 of 14 days nonzero, ₹4,990). An operator-chosen viewpoint 30 days earlier shows 8 of 14 days and ₹2,08,228.03 (`make demo-cash`). The report always labels which one you're seeing.
- **`false_match_rate` is computed against the engine's own asserted links**, not total records — deliberate, so it can't be gamed by asserting fewer links, but it must always be read next to auto-match rate.
- **`auto_match_rate` and `hands_off_rate` are currently identical** (see `eval/metrics.py`) — holds until a layer can leave a record neither matched nor exceptioned.
- **The fixture's UTR-truncation defect either leaves the UTR intact or removes it entirely** — L0's partial-prefix branch is exercised by unit test, not by `run_2000` itself.
- **Volume seasonality, ticket sizes, and defect rates are hand-tuned** to look like a mid-size D2C merchant; not calibrated against a real portfolio. The bank calendar covers 2025–2026 national holidays only, not state-specific ones.

---

## Everything else

| Document | What it answers |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | *Why it's built this way* — design rationale, every bug and the reasoning that caught it |
| [`RESULTS.md`](RESULTS.md) | *What's true right now* — every measured number, each naming its source file |

```bash
pip install -e ".[dev]"                                                        # +.[llm] for SDK-backed tests, +.[api] for the API
python -m data.generator.trace --fixtures data/fixtures/run_2000 --pick-clean   # hand-verify one full chain
python -m engine.l2_subset --profile --trials 2000 --seed 42                   # L2 solver timing
export NIM_API_KEY=...
python -m engine.l3_agent --profile --backend nim --model nvidia/nemotron-3-ultra-550b-a55b
```

`make gen`, `sample`, `test`, `trace`, `eval-null`, `eval-oracle`, `eval-l0l1`, `eval-l0l1l2`, `eval-full`, `l2-profile`, `l3-profile`, `demo`, `demo-cash`, `multiseed`, `adversarial`, `verify-deterministic`, `freeze`, `api`, `dashboard` wrap the same commands.
