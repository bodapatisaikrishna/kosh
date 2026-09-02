"""eval/report.py: the Phase 6 dashboard (a self-contained HTML report, per the
brief's own fallback preference over a separate Next.js app). Covers the new
cash panel, the exception drill-down data, and trace linking.
"""

from __future__ import annotations

from pathlib import Path

import eval.report as report_module
from eval.report import render_html, run_eval

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "sample_200"


def test_run_eval_includes_cash_and_exception_detail():
    report = run_eval(FIXTURES, "full")
    assert "cash" in report
    assert set(report["cash"]) >= {"as_of_date", "inflow_curve", "stuck_paise", "book_cash_paise", "reconciled_cash_paise", "reconciliation"}
    assert "exceptions_detail" in report
    assert len(report["exceptions_detail"]) == report["metrics"]["exceptions"]["count"]
    for e in report["exceptions_detail"]:
        assert e["recommended_action"]
        assert "trace_file" in e


def test_run_eval_reconciliation_ties_exactly():
    report = run_eval(FIXTURES, "full")
    cash = report["cash"]
    delta = cash["book_cash_paise"] - cash["reconciled_cash_paise"]
    assert delta - sum(cash["reconciliation"].values()) == 0


def _output_with(payment_id: str, category: str = "UNEXPLAINED_VARIANCE"):
    from engine.contract import ReconException

    exc = ReconException(
        category=category, severity="STANDARD", amount_at_risk_paise=100,
        affected={"payment_id": payment_id}, recommended_action="x",
    )

    class Output:
        exceptions = [exc]

    return Output()


def test_exception_gets_a_trace_link_when_a_matching_trace_file_exists(tmp_path, monkeypatch):
    traces_dir = tmp_path / "sample_traces"
    traces_dir.mkdir()
    (traces_dir / "pay_demo.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(report_module, "SAMPLE_TRACE_SOURCES", ((traces_dir, None),))

    result = report_module._exception_dicts(_output_with("pay_demo"), Path("data/fixtures/run_2000"))
    assert result[0]["trace_file"] == "sample_traces/pay_demo.json"


def test_exception_has_no_trace_link_when_no_file_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(report_module, "SAMPLE_TRACE_SOURCES", ((tmp_path / "nonexistent", None),))

    result = report_module._exception_dicts(_output_with("pay_none", "MISSING_SETTLEMENT"), Path("data/fixtures/run_2000"))
    assert result[0]["trace_file"] is None


def test_a_live_trace_links_on_the_fixture_it_was_actually_produced_from():
    """The 6 real run_2000 residual records have committed live agent traces in
    benchmarks/sample_traces_live/ - and the demo script's own climax beat
    ("click one exception -> full agent reasoning trace") depends on them
    actually rendering a link. Before this, SAMPLE_TRACES_DIR pointed only at
    the hand-built synthetic set, so all 6 rendered "no agent trace"."""
    report = run_eval(Path("data/fixtures/run_2000"), "full")
    unexplained = [e for e in report["exceptions_detail"] if e["category"] == "UNEXPLAINED_VARIANCE"]
    assert unexplained, "run_2000's L3 residual should still produce UNEXPLAINED_VARIANCE exceptions"
    linked = [e for e in unexplained if e["trace_file"]]
    assert len(linked) == len(unexplained), "every residual record with a committed live trace must link to it"
    for e in linked:
        assert e["trace_file"].startswith("sample_traces_live/")
        assert (Path("benchmarks") / e["trace_file"]).exists(), "a trace link must resolve to a real file, not a 404"


def test_a_live_trace_does_not_link_from_a_different_fixture(tmp_path, monkeypatch):
    """The regression guard for a real trap: record ids are seed-derived, so
    run_2000 and run_10000 share 2001 payment ids while holding genuinely
    different records. A filename-only lookup would show run_2000's real agent
    trace next to run_10000's unrelated payment of the same id - an LLM
    reasoning in detail about the wrong record, worse than no trace at all."""
    live_dir = tmp_path / "sample_traces_live"
    live_dir.mkdir()
    (live_dir / "pay_OyvjU0Hc7g7Bi2.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(report_module, "SAMPLE_TRACE_SOURCES", ((live_dir, "run_2000"),))

    same_fixture = report_module._exception_dicts(_output_with("pay_OyvjU0Hc7g7Bi2"), Path("data/fixtures/run_2000"))
    assert same_fixture[0]["trace_file"] == "sample_traces_live/pay_OyvjU0Hc7g7Bi2.json"

    other_fixture = report_module._exception_dicts(_output_with("pay_OyvjU0Hc7g7Bi2"), Path("data/fixtures/run_10000"))
    assert other_fixture[0]["trace_file"] is None, "a run_2000 trace must never link from run_10000's report"


def test_render_html_includes_all_four_panels():
    report = run_eval(FIXTURES, "full")
    out = render_html(report)
    assert "Layer waterfall" in out
    assert "Exception queue" in out
    assert "Cash position" in out
    assert "Rs reconciled" in out
    assert "inflow-chart" in out
    assert "function sortExceptions" in out


def test_category_header_actually_sorts_by_category_not_amount():
    # Regression: the "Category" header's onclick called sortExceptions()
    # with no argument, and the function only ever sorted by dataset.amount -
    # clicking "Category" silently re-sorted by rupee amount instead. Only a
    # substring check for "function sortExceptions" existed before, which
    # can't catch a header wired to the wrong column - this checks the
    # header actually names its own column.
    report = run_eval(FIXTURES, "full")
    out = render_html(report)
    assert 'onclick="sortExceptions(\'category\')">Category<' in out
    assert 'onclick="sortExceptions(\'amount\')">Rs at risk<' in out


def test_exception_rows_carry_both_sortable_data_attributes():
    report = run_eval(FIXTURES, "full")
    out = render_html(report)
    exc = report["exceptions_detail"][0]
    assert f'data-amount="{exc["amount_at_risk_paise"]}"' in out
    assert f'data-category="{exc["category"]}"' in out


def test_render_html_is_valid_enough_to_write(tmp_path):
    report = run_eval(FIXTURES, "full")
    out = render_html(report)
    path = tmp_path / "report.html"
    path.write_text(out, encoding="utf-8")
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_render_html_omits_llm_cost_card_when_l3_never_ran():
    # The `make demo` default path (engine=full, no client): llm_calls is 0,
    # so cost_usd_micros is 0 too - not because it's free, but because L3
    # never ran. Showing "$0.00" here would misleadingly read as "free".
    report = run_eval(FIXTURES, "full")
    assert report["metrics"]["throughput"]["llm_calls"] == 0
    out = render_html(report)
    assert "LLM cost" not in out


def test_render_html_shows_real_llm_cost_when_l3_ran():
    report = run_eval(FIXTURES, "full")
    report["metrics"]["throughput"]["llm_calls"] = 6
    report["metrics"]["throughput"]["cost_usd_micros"] = 74890
    report["metrics"]["throughput"]["cost_per_1000_records_micros"] = 40320
    out = render_html(report)
    assert "LLM cost" in out
    assert "$0.0749" in out
    assert "$0.0403" in out


def test_render_html_escapes_exception_content():
    report = run_eval(FIXTURES, "full")
    if report["exceptions_detail"]:
        report["exceptions_detail"][0]["recommended_action"] = "<script>alert(1)</script>"
        out = render_html(report)
        assert "<script>alert(1)</script>" not in out
