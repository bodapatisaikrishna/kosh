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
| 2 | Eval harness (metrics vs. ground truth) + null/oracle baselines | ✅ done (this repo) |
| 3 | L0 deterministic + L1 tolerance matching | not started |
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

## Phases 3–7 (design intent, not yet built)

- **L0/L1** (`engine/`): exact-key cascade (order→payment→settlement→UTR), then amount/date/narration-similarity tolerance matching with an ambiguity guard — ties escalate, they are never guessed.
- **L2**: subset-sum solver over same-day/T+1 candidate payments for a bank credit, with an explicit `AMBIGUOUS` result when more than one subset satisfies the target — refused, not picked.
- **L3**: a Claude agent restricted to a fixed toolset (`get_record`, `find_candidates`, `compute_expected_fee`, `explain_variance`, `solve_subset`, `propose_match`, `raise_exception`), structurally forbidden from asserting an ID it wasn't handed by a tool, and forbidden from doing its own arithmetic.
- **L4**: an exception ledger sorted by ₹ at risk — every unresolved item, no suppression.
- **Cash** (`cash/`): unsettled-payment SLA forecasting, a 14-day inflow curve, and reconciled-vs-book cash where the delta is fully explained by the exception ledger.

Full detail in [`KOSH_BUILD_PROMPT.md`](KOSH_BUILD_PROMPT.md).
