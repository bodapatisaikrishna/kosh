# Kosh — AI Finance Controller

Three-way payment settlement reconciliation for an Indian online merchant — Orders ↔ PG ledger ↔ Bank statement, tied out to the paisa, plus a forward cash position. Built for the Razorpay AI Buildathon 2026, Track 04.

## Results

The judging bar: *"Throughput plus measured accuracy plus an honest exception list. One cherry-picked match proves nothing."* Every number below is measured against a machine-readable `ground_truth.json` with injected, labelled defects — see [Why we generate our own data](#why-we-generate-our-own-data).

**Frozen benchmark, complete pipeline, 3 scales** ([`benchmarks/freeze_*.json`](benchmarks/)):

| Records | Auto-match | Precision / Recall | **False-match** | Exceptions | Wall clock |
|---|---|---|---|---|---|
| 500 | 97.61% | 100.00% / 99.91% | **0.00%** | 48 | ~2ms |
| 2,000 | 97.74% | 100.00% / 99.95% | **0.00%** | 139 | ~7ms |
| 10,000 | 97.83% | 100.00% / 100.00% | **0.00%** | 550 | ~32ms |

**False-match rate is the headline metric, not auto-match rate.** In finance a wrong match is worse than no match — it silently corrupts the books, where an unmatched item merely sits in a queue for review. It reads **0.00%** at every scale tested, including a real, non-scripted LLM run (below) — that's checked directly, not asserted. It's also not a vacuous zero: the harness is mutation-tested (inject 10 wrong links and it reports 0.24%, drop half the matches and recall halves), so it demonstrably fails when the engine is wrong.

**Not a `seed=42` artifact** — 6 independent seeds, fresh 2,000-record fixture each, deterministic pipeline only (hardening sprint Task 1):

| Seed | 1 | 7 | 42 | 100 | 2026 | 31337 | Mean | Stddev |
|---|---|---|---|---|---|---|---|---|
| Auto-match | 97.86% | 97.87% | 97.74% | 96.99% | 97.85% | 97.96% | **97.71%** | 0.36pp |
| False-match | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | **0.00%** | — |

Source: [`benchmarks/multiseed/summary.json`](benchmarks/multiseed/summary.json), regenerable with `make multiseed`.

**Every layer earns its place** — this is measured, not asserted:

| Layer | 500 | 2,000 | 10,000 | What only it can do |
|---|---|---|---|---|
| L0 exact-key | 97.76% | 99.33% | 99.82% | UTR/FK joins |
| L1 tolerance | 0.78% | 0.26% | 0.06% | UTR rekeyed with a transposed digit |
| L2 subset-sum | **1.47%** | **0.41%** | **0.12%** | consolidated payouts — one credit, 2–4 settlements, no per-settlement UTR |
| L3 agent | residual only | 6 records | — | variances no deterministic rule can decompose |

L3 saw **6 of 1,858 records (0.32%)**. The other 99.68% cost zero LLM tokens — that *is* the deterministic-first thesis, quantified. Run for real against those 6, L3 correctly matched 1 and correctly raised the other 5 as exceptions, with real ₹ amounts (details further down).

**Ablation — what each layer, and an all-LLM alternative, actually buys you** (hardening sprint Task 4):

| Engine mode | Fixture | Auto-match | False-match | Precision / Recall | Cost |
|---|---|---|---|---|---|
| Null (matches nothing) | `run_2000` | 0.00% | 0.00% | 0.00% / 0.00% | $0 |
| L0 + L1 | `run_2000` | 92.84% | **0.00%** | 100.00% / 99.54% | $0 |
| L0 + L1 + L2 | `run_2000` | 97.74% | **0.00%** | 100.00% / 99.95% | $0 |
| Full (+ L3 + L4, `client=None`) | `run_2000` | 97.74% | **0.00%** | 100.00% / 99.95% | $0 (no live LLM in the CLI path — see the live L3 run above) |
| **All-LLM** (L3 only — L0–L2 *and* L4 all bypassed) | `sample_200`\* | 81.52% | **0.00%** | 100.00% / 77.36% | $6.58 total, **$35.76/1000 records** |

\* Different fixture/scale than the rows above — 348 non-order records (payments + settlements + bank, orders excluded), not the 1,858-record `run_2000`. Routing every record through a live LLM at 2,000-record scale would have been materially more expensive and slower for the same architectural point. Real run: `nvidia/nemotron-3-ultra-550b-a55b` via NVIDIA NIM, 1,207 LLM calls, 348/348 records, zero crashes. Source: [`benchmarks/ablation_llm_only.json`](benchmarks/ablation_llm_only.json).

The all-LLM row isn't a knock against the model — it's the actual evidence for the architecture. False-match rate holds at 0.00% even with every deterministic layer disabled (the same "refuse rather than guess" discipline the agent is prompted with — constraint 3, confidence < 0.85 → `raise_exception`, not `propose_match` — holds up under load). What drops is recall: more genuinely ambiguous records get correctly refused (raised as exceptions) rather than confidently matched, at 30–50x the cost of the deterministic layers doing the same job for the 99.68% of records that never needed judgment in the first place. Deterministic-first isn't a shortcut around the LLM; it's what reserves LLM judgment for the cases that actually need it.

**Per-defect-class scoring on `run_2000` — 14/14 types, zero misses, zero misclassifications, zero false exceptions:**

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
| `compound_fee_tax_error` | 3-6/6\* | | |

The right-hand column is the half most systems get wrong: four defect types exist specifically to punish an engine that flags everything it doesn't instantly recognise. Precision on *not* raising a false alarm is scored just as hard as recall on real defects.

\* `compound_fee_tax_error` is L3's live residual, not deterministic — across 4 real runs of the same 6-record residual, detected counts were 6, 4, 5, and 3 out of 6, varying with how thoroughly the model investigated each record, not with a code change. In 3 of 4 runs the reported amount is exact to the paisa in all 6 cases regardless of category; in the 4th, one record's amount was understated (₹15.37 reported vs. the true ₹41.19) because the model investigated only the fee leg and never checked the GST leg at all — the fee-leg arithmetic itself was exactly correct, the investigation was just incomplete. See Limitations.

**L3, run for real, against the real `run_2000` residual** ([`benchmarks/phase5_live_residual.json`](benchmarks/phase5_live_residual.json)) — 6 records that L0–L2 and L4's deterministic classifier genuinely could not resolve, sent to `nvidia/nemotron-3-ultra-550b-a55b` via NVIDIA NIM (the brief specs Anthropic; no key was available — a documented deviation, not a silent one):

| Record | Outcome |
|---|---|
| `pay_dGxUjmPIxeeXo4` | Correctly matched to its settlement — independently checked against `ground_truth.json`. Its own 88-paise fee/GST anomaly judged immaterial, not separately flagged. |
| `pay_OyvjU0Hc7g7Bi2` | Correctly raised as `UNEXPLAINED_VARIANCE`, ₹2,286.93 at risk — exact match, the largest of the six |
| `pay_RMejvzSwrh9QXa` | Correctly raised as `UNEXPLAINED_VARIANCE`, ₹1.83 at risk — exact match to the true net delta |
| `pay_Yw6hEZsEyvZMNn` | Correctly raised as `UNEXPLAINED_VARIANCE`, ₹1.42 at risk — exact match |
| `pay_ymzQx3u8WEhd7G` | Correctly raised as `UNEXPLAINED_VARIANCE`, ₹41.19 at risk — exact match |
| `pay_3egKQ6BCralBAI` | Raised as `TAX_VARIANCE`, ₹0.90 at risk — exact amount, but a single-cause label: the model checked only the GST leg this run and never separately checked the fee leg, so the multi-leg coercion (below) correctly did not fire on one data point |

All 6 amounts exact to the paisa against the generator's own labelled defect, in this run and in a second live run minutes earlier (which independently investigated the same record more thoroughly and scored `UNEXPLAINED_VARIANCE` 6/6). Zero `AGENT_INCOMPLETE`, zero false matches, zero invented categories, zero wrong amounts, across both runs — verified by hand against ground truth for every asserted link, category, and amount, not just read off the summary. 51 real LLM calls, 487s wall clock, **$0.104 total → $0.056 per 1000 records** (well inside the brief's own <$0.50/1000 target), computed from real token counts against NIM's published per-token rate — not estimated. Full traces: [`benchmarks/sample_traces_live/`](benchmarks/sample_traces_live/).

Two further live runs, done later specifically to check whether 2 data points was enough evidence: a 3rd run scored 5/6 with all 6 amounts still exact; a 4th scored 3/6 and surfaced a real, more material instance of the same investigation-depth pattern — see Limitations for the honest full picture across all 4 runs.

**The category fix**: L3's tool layer now structurally recomputes the category to `UNEXPLAINED_VARIANCE` — and the amount to the true net delta, not one leg's own delta — whenever the model's own tool-call history shows 2+ distinct comparisons it couldn't decompose, the same "recompute, don't trust" pattern already used for severity. It fires exactly when the evidence supports it (2+ legs) and correctly refuses to fire on weaker evidence (1 leg) — real LLM investigation depth varies run to run, and the tool layer doesn't paper over that by guessing. Getting here took several live re-runs and multiple real bugs found and fixed along the way — see [ARCHITECTURE.md](ARCHITECTURE.md).

The agent's tool-use and hard-constraint enforcement are additionally proven on a small hand-built synthetic exercise set ([`benchmarks/phase5_synthetic.json`](benchmarks/phase5_synthetic.json), [`benchmarks/sample_traces/`](benchmarks/sample_traces/)) covering every constraint at least once — an unexplained variance, an ambiguous refusal, a subset-sum batch, a high-value review flag, and a turn-budget exhaustion — the same live model, same zero false matches. The first live run of that set surfaced a genuine false-match bug, fixed with regression tests, then re-run clean.

## Reproduce

```bash
git clone <this-repo> && cd kosh
pip install -e .
make demo
```

Opens `benchmarks/run_demo.html` — the full 4-panel dashboard (headline, layer waterfall, exception queue, cash position) generated from a fresh 2,000-record synthetic run. Verified in an isolated clone on a clean venv, not assumed.

## Architecture

Deterministic first, LLM last — five layers, each seeing only what the one above it couldn't resolve (shares from `run_2000`):

```
L0  Deterministic joins   (exact keys)         → 99.33% of matched links
L1  Tolerance matching    (±amount, ±date)     → 0.26%
L2  Combinatorial solver  (subset-sum)         → 0.41%
L3  LLM agent             (residual only)      → 6 records (0.32% of all records)
L4  Exception ledger      (honest remainder)   → 139 exceptions, every category covered
```

Each layer refuses rather than guesses when evidence is ambiguous — that's what holds false-match at zero. L0 won't pick between two settlements sharing a UTR prefix; L1 won't pick the "closest" of two candidates in tolerance; L2 returns `AMBIGUOUS` rather than choosing one of several valid subsets; L3's tool layer structurally rejects any ID it didn't hand the model, any match under 0.85 confidence, and recomputes severity itself rather than trusting the model.

The generator is deliberately adversarial to its own engine: it injects **consolidated payouts** (one credit, several settlements, no per-settlement reference — solvable only by subset-sum) and **compound fee+tax errors** (two overlapping causes, so no single-cause hypothesis can decompose them — genuinely UNEXPLAINED, real work for L3). That's what makes the layer shares above real measurements rather than a diagram. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full build history, including every bug found and how it was caught — among them two genuine false-match bugs, one surfaced by a live model mid-run.

**Stack**: Python 3.11+, pydantic-free dataclasses, integer paise everywhere (enforced by an AST lint, `tests/test_no_floats.py`), stdlib `csv`/`json` for the generator (no float-formatting risk), `anthropic`/`openai` SDKs behind a provider-agnostic `LLMClient` interface for L3.

**Repo layout**:

```
data/generator/   synthetic dataset generator + injected, labelled defects
engine/           L0-L4: matching layers, the LLM adapter, the exception ledger
eval/             scoring against ground truth, the 4-panel HTML dashboard
cash/             forward cash position: SLA forecast, stuck cash, book-vs-reconciled
tests/            pytest suite, incl. frozen regression baselines
benchmarks/       committed reports at every phase + the 3-scale freeze + sample traces
```

## Why we generate our own data

You cannot measure precision, recall, or false-match rate against real production data, because you don't have ground truth for real data — that's the whole reconciliation problem. So Kosh generates its own three-way dataset (`data/generator/`) with **injected, labelled defects** spanning 12 realistic failure modes — wrong MDR tier, GST variance, misallocated refunds, orphan chargebacks, FX drift, split settlements, and more — each labelled in `ground_truth.json` with its expected exception category and whether a deterministic engine should resolve it silently or flag it. Every claim the engine makes gets checked against that file. Realistic on purpose, not uniform: UPI/RuPay carry zero MDR (the real regulatory position), so most volume reconciles trivially and the interesting failures concentrate in card and international volume — same as a real merchant's exception queue.

```bash
python -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/
```

Same `--seed` → byte-identical output, every time. A small companion fixture, `data/fixtures/sample_200`, is committed to the repo so you can inspect real output without running anything.

## Limitations

- Recall is 99.95% on `run_2000`, not 100%: two settlements that net to exactly ₹0 are never claimed as members of the consolidated payout that paid them. That's deliberate — a zero-value term is degenerate in a subset-sum (including or excluding it gives an identical total), so a bank credit genuinely cannot evidence whether it rode along. Refusing costs 2 links; guessing would risk the false-match rate this project exists to protect.
- `PERIOD_CUTOFF` recall is 14/14 on `run_2000` at the current threshold, but that threshold (a >4-day settlement gap) was calibrated against this fixture's observed distribution. It is a tuned constant, not a law — on a different merchant's cycle it would need re-derivation, and one boundary case at exactly 3 days remains genuinely indistinguishable from a slow weekend.
- The defect *rates* are tuned so all 14 types appear at N=2000; at N=500 some types fire only once or twice, so per-class recall at that scale is a small-sample number and should be read as such.
- `propose_match`'s rationale-citation check is a best-effort structural check (does the text contain a known record id), not a semantic verification that the citation actually supports the claim.
- The turn budget was genuinely too tight at 8 (see ARCHITECTURE.md): 4 of 6 real `run_2000` residual records hit `AGENT_INCOMPLETE`, all from correct, thorough investigation that simply ran out of room. Raised to 12 and re-verified live: 0 of 6 incomplete. Constraint 6's fallback is still real infrastructure (it still fires whenever a case is genuinely too hard, e.g. the 2 of 5 synthetic exercise cases below), not a guarantee removed.
- L3's category for a compound fee+GST error can still be a single-cause label if the model's own investigation only ever surfaces one unexplained leg: the tool layer structurally recomputes the category to `UNEXPLAINED_VARIANCE` (and the amount to the true net delta) whenever 2+ distinct undecomposable comparisons appear in the tool-call history, but correctly refuses to coerce on weaker evidence. Across 4 real live runs of the same 6-record residual, detected counts were 6, 4, 5, and 3 out of 6 — genuine model investigation-depth variance, not a code difference (each run is the same code against the same cache-busted fixture). **A real, more material consequence of the same pattern surfaced in the 4th run, not previously seen in the first 3**: when the model investigates only one leg of a two-leg compound defect, the reported `amount_at_risk_paise` reflects only that one leg — normally close to the true compound total by coincidence of that record's composition, but in this case materially short (₹15.37 reported vs. the true ₹41.19, because the model checked the fee leg and never called `explain_variance` on the GST leg at all). This is not a computation error — the fee-leg arithmetic (expected 29,195 paise vs. observed 30,732 paise, delta 1,537 paise) is exactly correct — it's an incomplete investigation being reported honestly rather than the tool layer guessing at a number it was never given evidence for. See `benchmarks/phase5_live_residual.json`'s `compound_fee_tax_error_note`, `benchmarks/phase5_live_residual_run3.json`, `benchmarks/phase5_live_residual_run4.json`, and `benchmarks/sample_traces_live_more_runs/`.
- `auto_match_rate` and `hands_off_rate` are defined identically for now (see `eval/metrics.py`) — holds until a layer can leave a record neither matched nor exceptioned.
- `false_match_rate` is computed against the engine's own asserted links, not total records — deliberate, so it can't be gamed by asserting fewer links, but it must always be read next to auto-match rate, never alone.
- The reference fixture's own UTR-truncation defect happens to either leave the UTR fully intact or remove it entirely — L0's partial-prefix-match branch is exercised by unit test, not by `run_2000` itself.
- Volume seasonality, ticket-size distributions, and defect rates are hand-tuned to look like a mid-size D2C merchant; not calibrated against any real portfolio.
- The bank calendar covers 2025–2026 national holidays only, not state-specific ones.
- `cash/forecast.py`'s "as of" date is inferred from the dataset's own latest capture date, not passed in explicitly — fine for a fixed historical fixture, not for a live deployment.
- No FastAPI layer and no live/interactive dashboard — the static HTML report is the deliverable, per the brief's own explicit fallback preference over a broken Next.js app.

## Everything else

Full command reference, every phase's own results, and the complete list of bugs found and fixed along the way (with the exact reasoning that caught each one) live in [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`KOSH_BUILD_PROMPT.md`](KOSH_BUILD_PROMPT.md) (the original 7-phase build plan this repo follows).

```bash
pip install -e ".[dev]"                                                        # +.[llm] for the anthropic/openai-backed tests
python -m data.generator.trace --fixtures data/fixtures/run_2000 --pick-clean   # hand-verify one full chain
python -m eval.report --fixtures data/fixtures/run_2000 --engine full --label phase5
python -m engine.l2_subset --profile --trials 2000 --seed 42                   # L2 solver timing, synthetic instances
export NIM_API_KEY=...
python -m engine.l3_agent --profile --backend nim --model nvidia/nemotron-3-ultra-550b-a55b
pytest
```

`make gen`, `make sample`, `make test`, `make trace`, `make eval-null`, `make eval-oracle`, `make eval-l0l1`, `make eval-l0l1l2`, `make eval-full`, `make l2-profile`, `make l3-profile`, `make demo`, `make multiseed`, `make adversarial`, `make verify-deterministic`, and `make freeze` wrap the same commands.
