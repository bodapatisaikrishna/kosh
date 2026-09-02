"""Runs an engine against a fixture set, scores it, and emits:

    benchmarks/run_<label>.json   - full metrics, machine-readable
    benchmarks/run_<label>.html   - self-contained report (inline CSS, no build step)

    python -m eval.report --fixtures data/fixtures/run_2000 --engine oracle --label oracle_run2000

The JSON output separates "timing" (wall-clock, inherently non-deterministic) from
"metrics" (deterministic given the same engine + fixtures) so a regression test can
freeze the metrics half without fighting the clock.

This is the Phase 6 dashboard: per the brief's own fallback rule ("a working CLI
beats a broken dashboard, judges grade accuracy not CSS"), the four panels live
here as a self-contained static HTML report rather than a separate Next.js app -
headline strip, layer waterfall, a sortable exception queue with evidence-chain
and agent-trace drill-down, and the cash position.
"""

from __future__ import annotations

import argparse
import html
import json
import time
from dataclasses import asdict
from pathlib import Path

from cash.forecast import compute_forecast
from engine.baselines import null_baseline, oracle_baseline
from engine.io import load_dataset
from engine.pipeline import run_full, run_l0_l1, run_l0_l1_l2, run_llm_only
from eval.io import load_ground_truth
from eval.manifest import build_run_manifest
from eval.metrics import compute_metrics

ENGINES = {
    "null": lambda dataset, ground_truth: null_baseline(dataset),
    "oracle": lambda dataset, ground_truth: oracle_baseline(dataset, ground_truth),
    "l0l1": lambda dataset, ground_truth: run_l0_l1(dataset),
    "l0l1l2": lambda dataset, ground_truth: run_l0_l1_l2(dataset),
    "full": lambda dataset, ground_truth: run_full(dataset, client=None),
    # Deliberately client=None here too, same invariant as "full": the CLI must
    # never be able to accidentally spend real money. The real, costed ablation
    # run lives in scripts/run_ablation_llm_only.py.
    "llm-only": lambda dataset, ground_truth: run_llm_only(dataset, client=None),
}

# Where committed L3 traces live, and which fixture each set is valid for - an
# exception whose `affected` dict names a record with a trace here gets a
# drill-down link in the exception queue.
#
# The fixture scoping is load-bearing, not decoration. Record ids are derived
# from the generator's seed, so run_2000 and run_10000 (both seed=42) share
# 2001 payment ids while holding genuinely DIFFERENT records. Matching a trace
# on filename alone would link run_2000's real agent trace for
# pay_OyvjU0Hc7g7Bi2 from run_10000's dashboard, where that id is an unrelated
# FEE_VARIANCE payment - showing a judge an LLM reasoning in detail about the
# wrong record, which is strictly worse than showing no trace at all. A `None`
# fixture means the set is fixture-independent: sample_traces/ is the
# hand-built synthetic exercise set whose ids (pay_unexplained, btxn_batch)
# are written by hand and can never collide with a generated one.
SAMPLE_TRACE_SOURCES: tuple[tuple[Path, str | None], ...] = (
    (Path("benchmarks/sample_traces_live"), "run_2000"),
    (Path("benchmarks/sample_traces"), None),
)


def _exception_dicts(engine_output, fixtures_dir: Path) -> list[dict]:
    fixture_name = Path(fixtures_dir).name
    applicable = [(d, d.name) for d, fixture in SAMPLE_TRACE_SOURCES if fixture is None or fixture == fixture_name]

    out = []
    for e in engine_output.exceptions:
        d = asdict(e)
        d["trace_file"] = None
        for traces_dir, dir_name in applicable:
            # href is derived from the directory's own name rather than a
            # hardcoded string, so the two sets can't drift apart silently.
            trace_id = next((v for v in e.affected.values() if (traces_dir / f"{v}.json").exists()), None)
            if trace_id:
                d["trace_file"] = f"{dir_name}/{trace_id}.json"
                break
        out.append(d)
    return out


def run_eval(fixtures_dir: Path, engine_name: str, seed: int | None = None, model_name: str | None = None) -> dict:
    dataset = load_dataset(fixtures_dir)
    ground_truth = load_ground_truth(fixtures_dir)

    engine_fn = ENGINES[engine_name]
    started = time.time()
    engine_output = engine_fn(dataset, ground_truth)
    generated_at = started

    metrics = compute_metrics(dataset, engine_output, ground_truth)
    forecast = compute_forecast(dataset, engine_output.matches, engine_output.exceptions)
    manifest = build_run_manifest(fixtures_dir, engine_name, dataset, seed=seed, model_name=model_name)

    return {
        "engine": engine_name,
        "fixtures": str(fixtures_dir),
        "generated_at_unix": generated_at,
        "metrics": metrics,
        "exceptions_detail": _exception_dicts(engine_output, fixtures_dir),
        "cash": asdict(forecast),
        "manifest": manifest,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    return f"{sign}₹{abs(paise) / 100:,.2f}"


def _llm_cost_card(thr: dict) -> str:
    """A 6th headline card for real $ LLM cost - only when L3 actually ran
    (llm_calls > 0). Omitted otherwise so a deterministic-only run (the
    `make demo` default) never shows a misleading "$0.00", since that would
    read as "free" rather than "L3 didn't run"."""
    if not thr.get("llm_calls"):
        return ""
    cost_usd = thr["cost_usd_micros"] / 1_000_000
    per_1000_usd = thr["cost_per_1000_records_micros"] / 1_000_000
    return (
        '<div class="card"><div class="label">LLM cost (L3)</div>'
        f'<div class="value" style="font-size:1.4rem">${cost_usd:.4f}</div>'
        f'<div class="muted" style="font-size:0.7rem">${per_1000_usd:.4f} / 1000 records</div></div>'
    )


def render_html(report: dict) -> str:
    m = report["metrics"]
    acc = m["accuracy"]
    thr = m["throughput"]
    exc = m["exceptions"]
    cash = report["cash"]

    def esc(s: object) -> str:
        return html.escape(str(s))

    confusion_rows = "".join(
        f"<tr><td>{esc(dtype)}</td><td>{esc(json.dumps(counts))}</td></tr>"
        for dtype, counts in acc["defect_confusion"].items()
    )
    layer_rows = "".join(
        f"<tr><td>{esc(layer)}</td><td>{_pct(share)}</td></tr>" for layer, share in acc["layer_contribution"].items()
    )

    exception_rows = []
    for i, e in enumerate(sorted(report["exceptions_detail"], key=lambda x: x["amount_at_risk_paise"], reverse=True)):
        evidence_html = "".join(f"<li>{esc(line)}</li>" for line in e["evidence_chain"]) or "<li><em>no evidence recorded</em></li>"
        trace_html = (
            f'<a href="{esc(e["trace_file"])}" target="_blank">view agent trace &rarr;</a>'
            if e.get("trace_file") else '<span class="muted">no agent trace (deterministically classified)</span>'
        )
        exception_rows.append(f"""
        <tr class="exc-row" data-amount="{e['amount_at_risk_paise']}" data-category="{esc(e['category'])}" onclick="document.getElementById('detail-{i}').classList.toggle('open')">
          <td>{esc(e['category'])}</td>
          <td><span class="pill pill-{esc(e['severity'].lower())}">{esc(e['severity'])}</span></td>
          <td class="num">{_rupees(e['amount_at_risk_paise'])}</td>
          <td>{esc(e['suggested_owner'])}</td>
          <td>{esc(e['aging_days'])}d</td>
        </tr>
        <tr class="exc-detail" id="detail-{i}">
          <td colspan="5">
            <div class="detail-box">
              <strong>Affected:</strong> {esc(json.dumps(e['affected']))}<br>
              <strong>Recommended action:</strong> {esc(e['recommended_action'])}<br>
              <strong>Evidence chain:</strong>
              <ul>{evidence_html}</ul>
              {trace_html}
            </div>
          </td>
        </tr>""")
    exception_table_body = "".join(exception_rows) or '<tr><td colspan="5">no exceptions</td></tr>'

    manifest = report.get("manifest") or {}
    dirty = manifest.get("git_dirty")
    dirty_html = ' <strong style="color:#b00020">(dirty tree)</strong>' if dirty else ""
    sha = manifest.get("git_commit_sha") or "unknown"
    footer_html = (
        f'<p class="muted">commit <code>{esc(sha[:12] if sha != "unknown" else sha)}</code>{dirty_html} '
        f'&middot; generated {esc(manifest.get("generated_at_utc", "unknown"))}</p>'
    )

    max_inflow = max((row["expected_inflow_paise"] for row in cash["inflow_curve"]), default=0) or 1
    inflow_bars = "".join(
        f"""<div class="bar-col">
              <div class="bar" style="height:{max(2, (row['expected_inflow_paise'] * 100) // max_inflow)}%"></div>
              <div class="bar-label">{esc(row['date'][5:])}</div>
            </div>"""
        for row in cash["inflow_curve"]
    )
    reconciliation_rows = "".join(
        f"<tr><td>{esc(name)}</td><td class='num'>{_rupees(val)}</td></tr>" for name, val in cash["reconciliation"].items()
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Kosh eval report - {esc(report['engine'])}</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }}
  h1 {{ margin-bottom: 0.2rem; }}
  h2 {{ margin-bottom: 0.3rem; }}
  .subtitle {{ color: #666; margin-top: 0; }}
  .muted {{ color: #888; font-size: 0.85rem; }}
  .headline {{ display: flex; gap: 1.5rem; flex-wrap: wrap; margin: 1.5rem 0; }}
  .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.5rem; min-width: 160px; }}
  .card .label {{ font-size: 0.8rem; color: #666; text-transform: uppercase; letter-spacing: 0.04em; }}
  .card .value {{ font-size: 2rem; font-weight: 700; margin-top: 0.25rem; }}
  .card.risk .value {{ color: #b00020; }}
  table {{ border-collapse: collapse; margin: 0.5rem 0 1.5rem; width: 100%; max-width: 720px; }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.7rem; text-align: left; font-size: 0.9rem; }}
  th {{ background: #f5f5f5; cursor: pointer; user-select: none; }}
  th.sortable:hover {{ background: #e8e8e8; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  section {{ margin-bottom: 2.5rem; }}
  .exc-row {{ cursor: pointer; }}
  .exc-row:hover {{ background: #fafafa; }}
  .exc-detail {{ display: none; }}
  .exc-detail.open {{ display: table-row; }}
  .detail-box {{ background: #f9f9f9; border-left: 3px solid #b00020; padding: 0.6rem 1rem; font-size: 0.85rem; }}
  .pill {{ display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }}
  .pill-standard {{ background: #eee; color: #444; }}
  .pill-review_required {{ background: #fde3e3; color: #b00020; }}
  .inflow-chart {{ display: flex; align-items: flex-end; gap: 4px; height: 120px; max-width: 720px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  .bar-col {{ flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; height: 100%; }}
  .bar {{ width: 100%; background: #2b6cb0; border-radius: 2px 2px 0 0; min-height: 2px; }}
  .bar-label {{ font-size: 0.65rem; color: #888; margin-top: 4px; writing-mode: vertical-rl; }}
</style>
</head>
<body>
  <h1>Kosh reconciliation - eval report</h1>
  <p class="subtitle">engine: <strong>{esc(report['engine'])}</strong> &middot; fixtures: <code>{esc(report['fixtures'])}</code></p>

  <div class="headline">
    <div class="card"><div class="label">Records processed</div><div class="value">{thr['records_processed']}</div></div>
    <div class="card"><div class="label">Auto-match rate</div><div class="value">{_pct(acc['auto_match_rate'])}</div></div>
    <div class="card risk"><div class="label">False-match rate</div><div class="value">{_pct(acc['false_match_rate'])}</div></div>
    <div class="card"><div class="label">Rs reconciled</div><div class="value" style="font-size:1.4rem">{_rupees(cash['reconciled_cash_paise'])}</div></div>
    <div class="card"><div class="label">Wall clock</div><div class="value">{thr['wall_clock_seconds']:.3f}s</div></div>
    {_llm_cost_card(thr)}
  </div>

  <section>
    <h2>Layer waterfall</h2>
    <p class="muted">Share of correctly-matched links contributed by each layer.</p>
    <table><tr><th>Layer</th><th>Share of correct matches</th></tr>{layer_rows or '<tr><td colspan="2">no matches</td></tr>'}</table>
  </section>

  <section>
    <h2>Exception queue</h2>
    <p>{exc['count']} exceptions &middot; {_rupees(exc['total_amount_at_risk_paise'])} at risk &middot; <span class="muted">click a row for evidence + trace</span></p>
    <table>
      <tr><th class="sortable" onclick="sortExceptions('category')">Category</th><th>Severity</th><th class="sortable num" onclick="sortExceptions('amount')">Rs at risk</th><th>Owner</th><th>Aging</th></tr>
      {exception_table_body}
    </table>
  </section>

  <section>
    <h2>Cash position</h2>
    <p class="muted">As of {esc(cash['as_of_date'])} &middot; Rs stuck: <strong>{_rupees(cash['stuck_paise'])}</strong> ({len(cash['stuck_payment_ids'])} payments) &middot; Rs at risk: <strong>{_rupees(cash['at_risk_paise'])}</strong></p>
    <h3 style="font-size:0.95rem;margin-bottom:0.3rem;">14-day expected inflow</h3>
    <div class="inflow-chart">{inflow_bars}</div>
    <h3 style="font-size:0.95rem;margin:1.2rem 0 0.3rem;">Book cash vs. reconciled cash</h3>
    <p class="muted">Book: {_rupees(cash['book_cash_paise'])} &middot; Reconciled: {_rupees(cash['reconciled_cash_paise'])} &middot; every paisa of the gap is named below</p>
    <table><tr><th>Component</th><th class="num">Amount</th></tr>{reconciliation_rows}</table>
  </section>

  <section>
    <h2>Per-defect-class confusion</h2>
    <table><tr><th>Defect type</th><th>Outcome counts</th></tr>{confusion_rows}</table>
  </section>

  <footer>{footer_html}</footer>

<script>
  // Per-column ascending state, so sorting by category then by amount doesn't
  // share one toggle (each column remembers its own direction independently).
  const sortAscByColumn = {{amount: true, category: true}};
  function sortExceptions(column) {{
    // column is "amount" (the brief's own explicit ask: "sortable by Rs at
    // risk") or "category" (the header's own label - previously wired to
    // amount regardless of which header you clicked, a real bug: the
    // "Category" header silently re-sorted by rupee amount, which looked
    // plausible only because the table's default order is already
    // amount-descending).
    const table = document.querySelectorAll("table")[1];
    const rows = Array.from(table.querySelectorAll("tr.exc-row"));
    const asc = sortAscByColumn[column];
    rows.sort((a, b) => {{
      const diff = column === "amount"
        ? Number(a.dataset.amount) - Number(b.dataset.amount)
        : a.dataset.category.localeCompare(b.dataset.category);
      return asc ? diff : -diff;
    }});
    sortAscByColumn[column] = !asc;
    for (const row of rows) {{
      const detail = row.nextElementSibling;
      table.appendChild(row);
      table.appendChild(detail);
    }}
  }}
</script>
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
