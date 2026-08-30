# Kosh — AI Finance Controller

Three-way payment settlement reconciliation for an Indian online merchant — Orders ↔ PG ledger ↔ Bank statement, tied out to the paisa, plus a forward cash position. Built for the Razorpay AI Buildathon 2026, Track 04.

## Results

The judging bar: *"Throughput plus measured accuracy plus an honest exception list. One cherry-picked match proves nothing."* Every number below is measured against a machine-readable `ground_truth.json` with injected, labelled defects — see [Why we generate our own data](#why-we-generate-our-own-data).

**Frozen benchmark, complete pipeline, 3 scales** ([`benchmarks/freeze_*.json`](benchmarks/)):

| Records | Auto-match | Precision / Recall | **False-match** | Exceptions | Wall clock |
|---|---|---|---|---|---|
| 500 | 97.83% | 100.00% / 100.00% | **0.00%** | 47 | ~2ms |
| 2,000 | 97.85% | 100.00% / 100.00% | **0.00%** | 132 | ~7ms |
| 10,000 | 97.83% | 100.00% / 100.00% | **0.00%** | 521 | ~31ms |

**False-match rate is the headline metric, not auto-match rate.** In finance a wrong match is worse than no match — it silently corrupts the books, where an unmatched item merely sits in a queue for review. It reads **0.00%** at every scale tested, including a real, non-scripted LLM run (below) — that's checked directly, not asserted.

**L3, run for real against a live model.** `run_2000`'s residual for the agent is empty — L0–L2 and L4's deterministic classifier already resolve everything, the same honest "nothing left to do" story L2 tells on its own. So the agent's tool-use and constraint enforcement were proven on a small synthetic exercise set instead, run against `nvidia/nemotron-3-ultra-550b-a55b` via NVIDIA NIM (the brief specs Anthropic; no key was available — a documented deviation, not a silent one). The first live run surfaced a genuine false-match bug, fixed with regression tests, then re-run clean:

| Record | Outcome |
|---|---|
| Genuinely unexplained variance | Correctly raised `UNEXPLAINED_VARIANCE` |
| Two identical-amount settlement candidates | Correctly refused to guess |
| A real 2-settlement batch | Correctly chained `find_candidates` → `solve_subset` → `propose_match`, both linked |
| 2 records needing deeper investigation | `AGENT_INCOMPLETE` — thorough, correct reasoning that ran past its 8-turn budget |

Zero false matches. Full traces: [`benchmarks/sample_traces/`](benchmarks/sample_traces/).

## Reproduce

```bash
git clone <this-repo> && cd kosh
pip install -e .
make demo
```

Opens `benchmarks/run_demo.html` — the full 4-panel dashboard (headline, layer waterfall, exception queue, cash position) generated from a fresh 2,000-record synthetic run. Verified in an isolated clone on a clean venv, not assumed.

## Architecture

Deterministic first, LLM last — five layers, each seeing only what the one above it couldn't resolve:

```
L0  Deterministic joins   (exact keys)         → 99.7% of matched links
L1  Tolerance matching    (±amount, ±date)     → 0.3%
L2  Combinatorial solver  (subset-sum)         → tested, 0% on this fixture (see below)
L3  LLM agent             (residual only)      → tested live, 0% on this fixture (see below)
L4  Exception ledger      (honest remainder)   → 132 exceptions, every category covered
```

L2 and L3 both report **zero contribution on the reference fixture** — not a bug, the honest result of L0/L1/L4 already resolving everything real production data would need them for. Both are still fully built and independently verified: L2 against 2,000 synthetic subset-sum instances (`benchmarks/phase4_solver_perf.json`), L3 against the live NIM run above. A system that only ever showed you the happy path wouldn't have caught either of the two false-match bugs this build actually found and fixed (`propose_match`'s link-type inference; `eval/metrics.py`'s defect-confusion matching) — see [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full build history, including every bug found and how it was caught, phase by phase.

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

- L2 and L3 both have no real residual to work on in the reference fixture — their correctness and timing are verified by direct unit/perf tests and the live NIM run, not by `run_2000`'s own numbers. See Architecture above.
- `PERIOD_CUTOFF` recall is 13/14 (92.9%) on `run_2000` — one boundary case (a 3-day settlement gap) is genuinely indistinguishable from ~20 normal same-gap settlements using the available signal; reported as a miss rather than guessed at the cost of false positives.
- `propose_match`'s rationale-citation check is a best-effort structural check (does the text contain a known record id), not a semantic verification that the citation actually supports the claim.
- Two of the five live L3 exercise records ended in `AGENT_INCOMPLETE` — both were thorough, correct investigation that simply didn't fit inside the 8-turn budget. That's constraint 6's infrastructure-level fallback working as designed, not a false positive, but it's an honest sign the turn budget is tight for a genuinely uncertain case.
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

`make gen`, `make sample`, `make test`, `make trace`, `make eval-null`, `make eval-oracle`, `make eval-l0l1`, `make eval-l0l1l2`, `make eval-full`, `make l2-profile`, `make l3-profile`, and `make demo` wrap the same commands.
