# Kosh dashboard

The brief's own optional "if time allows" stretch goal — a live, interactive
frontend for Kosh's reconciliation engine, built after the main project's
code freeze. See the root [`RESULTS.md`](../RESULTS.md)'s "Post-submission
stretch goals" section and [`ARCHITECTURE.md`](../ARCHITECTURE.md)'s matching
section for the full design story.

The static HTML report (`make demo`, from the repo root) remains the primary
deliverable and needs nothing but Python. This is additive.

## Run it

From the repo root, in two terminals:

```bash
make api        # FastAPI on :8000
make dashboard  # this app, on :3000
```

Or directly from this directory (with the API already running):

```bash
npm install
npm run dev
```

## What it does

- **Live run**: pick an engine/records/seed/months, click "Run live" — a real
  `generate → reconcile → score` pass runs through `api/main.py`'s
  `POST /api/runs`, which is a thin wrapper around the exact same
  `eval.report.run_eval` the CLI already calls.
- **Historical benchmarks**: browse the already-frozen `freeze_500/2000/10000`
  and `phase3/4/5` results without regenerating anything.
- Both render the same four panels: headline cards, a layer-waterfall chart,
  a sortable click-to-expand exception queue (with real agent-trace
  drill-down when one exists), and the cash position.

Never reachable from here: the costed live-LLM engine (`llm-only`) — see
`../api/runs.py`'s `ENGINE_ALLOWLIST`. This app talks to `http://localhost:8000`
only; it is not deployed anywhere.
