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


def test_exception_gets_a_trace_link_when_a_matching_trace_file_exists(tmp_path, monkeypatch):
    traces_dir = tmp_path / "sample_traces"
    traces_dir.mkdir()
    (traces_dir / "pay_demo.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(report_module, "SAMPLE_TRACES_DIR", traces_dir)

    from engine.contract import ReconException
    real_exc = ReconException(category="UNEXPLAINED_VARIANCE", severity="STANDARD", amount_at_risk_paise=100, affected={"payment_id": "pay_demo"}, recommended_action="x")

    class Output:
        exceptions = [real_exc]

    result = report_module._exception_dicts(Output())
    assert result[0]["trace_file"] == "sample_traces/pay_demo.json"


def test_exception_has_no_trace_link_when_no_file_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(report_module, "SAMPLE_TRACES_DIR", tmp_path / "nonexistent")
    from engine.contract import ReconException
    real_exc = ReconException(category="MISSING_SETTLEMENT", severity="STANDARD", amount_at_risk_paise=100, affected={"payment_id": "pay_none"}, recommended_action="x")

    class Output:
        exceptions = [real_exc]

    result = report_module._exception_dicts(Output())
    assert result[0]["trace_file"] is None


def test_render_html_includes_all_four_panels():
    report = run_eval(FIXTURES, "full")
    out = render_html(report)
    assert "Layer waterfall" in out
    assert "Exception queue" in out
    assert "Cash position" in out
    assert "Rs reconciled" in out
    assert "inflow-chart" in out
    assert "function sortExceptions" in out


def test_render_html_is_valid_enough_to_write(tmp_path):
    report = run_eval(FIXTURES, "full")
    out = render_html(report)
    path = tmp_path / "report.html"
    path.write_text(out, encoding="utf-8")
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_render_html_escapes_exception_content():
    report = run_eval(FIXTURES, "full")
    if report["exceptions_detail"]:
        report["exceptions_detail"][0]["recommended_action"] = "<script>alert(1)</script>"
        out = render_html(report)
        assert "<script>alert(1)</script>" not in out
