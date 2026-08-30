# Kosh — AI Finance Controller

Three-way payment settlement reconciliation for an Indian online merchant: **Orders ↔ PG ledger ↔ Bank statement**, tied out to the paisa, plus a forward cash position.

Built for the Razorpay AI Buildathon 2026, Track 04.

> **Status: Phase 3 of 7.** This repo ships the synthetic data generator with ground truth, the eval harness, and now L0 (exact-key joins) + L1 (tolerance matching). See [`KOSH_BUILD_PROMPT.md`](KOSH_BUILD_PROMPT.md) for the full 7-phase build plan and [`ARCHITECTURE.md`](ARCHITECTURE.md) for the target system design.

## Results — Phase 3 (L0 + L1), `data/fixtures/run_2000`

| Metric | Value |
|---|---|
| Auto-match rate | **97.85%** |
| Precision / Recall | **100.00% / 100.00%** |
| **False-match rate** | **0.00%** |
| Wall clock (2000 records) | **~4ms** |
| Layer split | L0 99.74% · L1 0.26% |

Every one of the 1,858 captured payments not affected by `missing_settlement` or `duplicate_payment` (which have no true settlement link to begin with) is matched correctly, with zero wrong links asserted. This exactly matches the oracle baseline's own auto-match rate on the same fixture — L0+L1 achieves parity with an engine that reads the ground truth directly, without ever seeing it. See [`benchmarks/phase3.json`](benchmarks/phase3.json) / [`.html`](benchmarks/phase3.html) for the full report.

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

```bash
pip install -e ".[dev]"
python -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/
python -m data.generator.trace --fixtures data/fixtures/run_2000 --pick-clean   # hand-verify one full chain
python -m eval.report --fixtures data/fixtures/run_2000 --engine l0l1 --label phase3
pytest
```

`make gen`, `make sample`, `make test`, `make trace`, `make eval-null`, `make eval-oracle`, and `make eval-l0l1` wrap the same commands.

## Repo layout

```
data/generator/   synthetic dataset generator (Phase 1)
engine/           EngineOutput contract (Phase 2), null/oracle baselines, L0 + L1 (Phase 3); L2-L4 not yet built
eval/             eval harness: metrics against ground truth, benchmark reports (Phase 2)
cash/             forward cash position (not yet built)
api/              FastAPI layer (not yet built)
tests/            pytest suite, incl. tests/baselines/ regression fixtures
benchmarks/       committed example reports: null/oracle baselines, phase3
```

## What's in Phase 3: L0 + L1

`engine/l0_deterministic.py` cascades three exact-key joins: `orders.order_id ↔ pg_payments.order_id`, `pg_payments.settlement_id ↔ pg_settlements.settlement_id`, and `pg_settlements.utr ↔` a UTR extracted from `bank_statement.narration` (via `engine/normalize.py`). UTR extraction handles a full 16-character match, a truncated prefix (≥12 chars, only if it uniquely identifies one settlement), and refuses to guess otherwise. `engine/l1_tolerance.py` catches whatever L0 couldn't place — chiefly a bank credit whose UTR was rekeyed with a transposed digit — by requiring amount within ±₹3, date within ±3 days, *and* narration that reads as a genuine settlement credit, all three, and only when exactly one settlement satisfies them.

`engine/fees.py` re-exports the generator's fee model unchanged (so a fee bug is a generator-test failure, never a fake match) and adds `explain_variance`, which decomposes an observed-vs-expected paise delta into a known cause — rounding, a wrong fee tier, a wrong GST rate, a known refund — or honestly returns `UNEXPLAINED`. This is the function Phase 5's exception ledger will lean on.

Two ambiguity guards are load-bearing, not incidental: an L0 UTR-prefix match that fits more than one settlement is refused, and an L1 candidate set with more than one plausible settlement is refused — both escalate rather than pick. `tests/test_l0_l1.py` exercises both directly (constructed cases, since the reference fixture doesn't happen to produce a genuine collision) alongside the Phase 3 checkpoint on `run_2000`.

## Limitations (Phase 1–3)

- L2 (subset-sum), L3 (Claude agent), and L4 (exception ledger) don't exist yet. Whatever L0/L1 can't place is simply absent from the match list for now — Phase 3's pipeline reports zero exceptions, which is a known temporary gap, not a claim that everything reconciled.
- `auto_match_rate` and `hands_off_rate` are defined identically for now (see `eval/metrics.py` module docstring) — a documented simplification that holds until a layer can leave a record neither matched nor exceptioned.
- `false_match_rate` is computed against the engine's own asserted links (wrong / total asserted), not against total records — deliberate, so it can't be gamed by asserting very few links, but it means it must always be read next to auto-match rate, never alone.
- The reference fixture's own truncation defect happens to either leave the UTR fully intact or remove it entirely (its longest template prefix already exceeds the 35-char cutoff) — L0's partial-prefix-match branch is exercised by unit test, not by `run_2000` itself.
- Volume seasonality, ticket-size distributions, and defect rates are hand-tuned to look like a mid-size D2C merchant; they are not calibrated against any real portfolio.
- The bank calendar covers 2025–2026 national holidays only, not state-specific ones.
