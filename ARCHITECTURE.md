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
| 1 | Synthetic data generator + ground truth | ✅ done (this repo) |
| 2 | Eval harness (metrics vs. ground truth) | not started |
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

## Phases 2–7 (design intent, not yet built)

- **Eval harness** (`eval/`): throughput (records/sec, LLM cost), accuracy (auto-match rate, per-layer contribution, false-match rate, precision/recall, per-defect-class confusion matrix), and an honest exception summary (count, ₹ at risk, category breakdown). Headline metric: **false-match rate** — in finance a wrong match is worse than no match, because it silently corrupts the books.
- **L0/L1** (`engine/`): exact-key cascade (order→payment→settlement→UTR), then amount/date/narration-similarity tolerance matching with an ambiguity guard — ties escalate, they are never guessed.
- **L2**: subset-sum solver over same-day/T+1 candidate payments for a bank credit, with an explicit `AMBIGUOUS` result when more than one subset satisfies the target — refused, not picked.
- **L3**: a Claude agent restricted to a fixed toolset (`get_record`, `find_candidates`, `compute_expected_fee`, `explain_variance`, `solve_subset`, `propose_match`, `raise_exception`), structurally forbidden from asserting an ID it wasn't handed by a tool, and forbidden from doing its own arithmetic.
- **L4**: an exception ledger sorted by ₹ at risk — every unresolved item, no suppression.
- **Cash** (`cash/`): unsettled-payment SLA forecasting, a 14-day inflow curve, and reconciled-vs-book cash where the delta is fully explained by the exception ledger.

Full detail in [`KOSH_BUILD_PROMPT.md`](KOSH_BUILD_PROMPT.md).
