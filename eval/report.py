"""Runs an engine against a fixture set, scores it, and emits:

    benchmarks/run_<label>.json   - full metrics, machine-readable
    benchmarks/run_<label>.html   - self-contained report (inline CSS, no build step)

    python -m eval.report --fixtures data/fixtures/run_2000 --engine oracle --label oracle_run2000

The JSON output separates "timing" (wall-clock, inherently non-deterministic) from
"metrics" (deterministic given the same engine + fixtures) so a regression test can
freeze the metrics half without fighting the clock.
"""

from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path

from engine.baselines import null_baseline, oracle_baseline
from engine.io import load_dataset
from engine.pipeline import run_full, run_l0_l1, run_l0_l1_l2
from eval.io import load_ground_truth
from eval.metrics import compute_metrics

ENGINES = {
    "null": lambda dataset, ground_truth: null_baseline(dataset),
    "oracle": lambda dataset, ground_truth: oracle_baseline(dataset, ground_truth),
    "l0l1": lambda dataset, ground_truth: run_l0_l1(dataset),
    "l0l1l2": lambda dataset, ground_truth: run_l0_l1_l2(dataset),
    "full": lambda dataset, ground_truth: run_full(dataset, client=None),
}


def run_eval(fixtures_dir: Path, engine_name: str) -> dict:
    dataset = load_dataset(fixtures_dir)
    ground_truth = load_ground_truth(fixtures_dir)

    engine_fn = ENGINES[engine_name]
    started = time.time()
    engine_output = engine_fn(dataset, ground_truth)
    generated_at = started

    metrics = compute_metrics(dataset, engine_output, ground_truth)
    return {
        "engine": engine_name,
        "fixtures": str(fixtures_dir),
        "generated_at_unix": generated_at,
        "metrics": metrics,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def render_html(report: dict) -> str:
    m = report["metrics"]
    acc = m["accuracy"]
    thr = m["throughput"]
    exc = m["exceptions"]

    def esc(s: object) -> str:
        return html.escape(str(s))

    confusion_rows = "".join(
        f"<tr><td>{esc(dtype)}</td><td>{esc(json.dumps(counts))}</td></tr>"
        for dtype, counts in acc["defect_confusion"].items()
    )
    category_rows = "".join(
        f"<tr><td>{esc(cat)}</td><td>{count}</td></tr>" for cat, count in exc["by_category"].items()
    )
    layer_rows = "".join(
        f"<tr><td>{esc(layer)}</td><td>{_pct(share)}</td></tr>" for layer, share in acc["layer_contribution"].items()
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Kosh eval report - {esc(report['engine'])}</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }}
  h1 {{ margin-bottom: 0.2rem; }}
  .subtitle {{ color: #666; margin-top: 0; }}
  .headline {{ display: flex; gap: 1.5rem; flex-wrap: wrap; margin: 1.5rem 0; }}
  .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.5rem; min-width: 160px; }}
  .card .label {{ font-size: 0.8rem; color: #666; text-transform: uppercase; letter-spacing: 0.04em; }}
  .card .value {{ font-size: 2rem; font-weight: 700; margin-top: 0.25rem; }}
  .card.risk .value {{ color: #b00020; }}
  table {{ border-collapse: collapse; margin: 0.5rem 0 1.5rem; width: 100%; max-width: 640px; }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.7rem; text-align: left; font-size: 0.9rem; }}
  th {{ background: #f5f5f5; }}
  section {{ margin-bottom: 2rem; }}
</style>
</head>
<body>
  <h1>Kosh reconciliation - eval report</h1>
  <p class="subtitle">engine: <strong>{esc(report['engine'])}</strong> &middot; fixtures: <code>{esc(report['fixtures'])}</code></p>

  <div class="headline">
    <div class="card"><div class="label">Records processed</div><div class="value">{thr['records_processed']}</div></div>
    <div class="card"><div class="label">Auto-match rate</div><div class="value">{_pct(acc['auto_match_rate'])}</div></div>
    <div class="card risk"><div class="label">False-match rate</div><div class="value">{_pct(acc['false_match_rate'])}</div></div>
    <div class="card"><div class="label">Precision / Recall</div><div class="value">{_pct(acc['precision'])} / {_pct(acc['recall'])}</div></div>
    <div class="card"><div class="label">Wall clock</div><div class="value">{thr['wall_clock_seconds']:.3f}s</div></div>
  </div>

  <section>
    <h2>Layer waterfall</h2>
    <table><tr><th>Layer</th><th>Share of correct matches</th></tr>{layer_rows or '<tr><td colspan="2">no matches</td></tr>'}</table>
  </section>

  <section>
    <h2>Exception queue</h2>
    <p>{exc['count']} exceptions &middot; ₹{exc['total_amount_at_risk_paise'] / 100:,.2f} at risk</p>
    <table><tr><th>Category</th><th>Count</th></tr>{category_rows or '<tr><td colspan="2">none</td></tr>'}</table>
  </section>

  <section>
    <h2>Per-defect-class confusion</h2>
    <table><tr><th>Defect type</th><th>Outcome counts</th></tr>{confusion_rows}</table>
  </section>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run an engine against fixtures and emit a benchmark report.")
    parser.add_argument("--fixtures", required=True, type=str)
    parser.add_argument("--engine", required=True, choices=sorted(ENGINES))
    parser.add_argument("--label", type=str, default=None, help="defaults to '<engine>_<fixtures-basename>'")
    parser.add_argument("--out", type=str, default="benchmarks")
    args = parser.parse_args(argv)

    fixtures_dir = Path(args.fixtures)
    label = args.label or f"{args.engine}_{fixtures_dir.name}"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = run_eval(fixtures_dir, args.engine)
    (out_dir / f"run_{label}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / f"run_{label}.html").write_text(render_html(report), encoding="utf-8")
    print(f"wrote {out_dir / f'run_{label}.json'} and .html")


if __name__ == "__main__":
    main()
