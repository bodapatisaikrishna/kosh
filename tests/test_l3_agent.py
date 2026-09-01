"""engine/l3_agent.py: the tool-call loop, tested entirely against FakeClient -
no network. Covers the "always close the loop" enforcement (constraint 6), the
reminder-then-retry path, rate-limit backoff, caching, and trace persistence.
"""

from __future__ import annotations

from pathlib import Path


from engine.fees import compute_expected_fee
from engine.io import BankRow, Dataset, OrderRow, PaymentRow, SettlementRow
from engine.l3_agent import run_agent_on_record, run_l3
from engine.llm.base import AssistantTurn, ToolCall, TransientBackendError
from engine.llm.fake_client import FakeClient


def _dataset():
    order = OrderRow("order_1", "2026-08-01", "cust_1", 100_000_00, "INR", "upi", "paid", "INV-1")
    fee, gst, net = compute_expected_fee(100_000_00, "upi", False)
    payment = PaymentRow("pay_1", "order_1", "2026-08-01T10:00:00", "upi", False, 100_000_00, fee, gst, net, "captured", None, None, 0)
    settlement = SettlementRow("setl_1", "2026-08-03", "HDFCN00000000001", 1, 100_000_00, fee, gst, 0, net)
    bank = BankRow("btxn_1", "2026-08-03", "NEFT-HDFCN00000000001-RAZORPAY SOFTWARE PVT LTD", net, 0, 0)
    return Dataset(orders=[order], payments=[payment], settlements=[settlement], bank=[bank])


def _dirs(tmp_path: Path) -> dict:
    return {"cache_dir": tmp_path / "cache", "traces_dir": tmp_path / "traces"}


def test_immediate_raise_exception_closes_the_loop_in_one_turn(tmp_path):
    client = FakeClient([
        AssistantTurn(text=None, tool_calls=(
            ToolCall(id="tc_1", name="raise_exception", arguments={
                "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 5_000_00,
                "recommended_action": "investigate", "rationale": "pay_1's net does not match any known cause",
            }),
        ), stop_reason="tool_use"),
    ])
    trace = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", **_dirs(tmp_path))
    assert trace.final_decision == "exception"
    assert trace.result["exception"]["category"] == "UNEXPLAINED_VARIANCE"
    assert trace.llm_calls == 1
    assert client.call_count == 1


def test_propose_match_closes_the_loop(tmp_path):
    client = FakeClient([
        AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="get_record", arguments={"source": "settlements", "record_id": "setl_1"}),), stop_reason="tool_use"),
        AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_2", name="propose_match", arguments={
            "record_ids": ["pay_1", "setl_1"], "confidence": 0.95, "rationale": "setl_1 matches pay_1 exactly",
        }),), stop_reason="tool_use"),
    ])
    trace = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", **_dirs(tmp_path))
    assert trace.final_decision == "match"
    assert len(trace.result["matches"]) == 1
    assert trace.llm_calls == 2


def test_ending_a_turn_without_a_tool_call_gets_a_reminder_then_retries(tmp_path):
    client = FakeClient([
        AssistantTurn(text="I'm thinking about it.", tool_calls=(), stop_reason="end_turn"),
        AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
            "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
            "recommended_action": "x", "rationale": "still unexplained after reflection",
        }),), stop_reason="tool_use"),
    ])
    trace = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", **_dirs(tmp_path))
    assert trace.final_decision == "exception"
    assert len(trace.turns) == 2
    # the reminder was actually sent as a user message between the two turns
    second_call_messages = client.received_messages[1]
    assert any("close this investigation" in (m.content or "") for m in second_call_messages)


def test_never_closing_the_loop_forces_an_agent_incomplete_exception(tmp_path):
    client = FakeClient([AssistantTurn(text="still thinking", tool_calls=(), stop_reason="end_turn") for _ in range(3)])
    trace = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", max_turns=3, **_dirs(tmp_path))
    assert trace.final_decision == "exception"
    assert trace.result["exception"]["category"] == "AGENT_INCOMPLETE"
    assert trace.result["exception"]["recommended_action"]  # never empty, even on the forced path


def test_rate_limit_is_retried_with_backoff(tmp_path, monkeypatch):
    import engine.l3_agent as l3_agent
    monkeypatch.setattr(l3_agent.time, "sleep", lambda s: None)  # keep the test fast

    client = FakeClient(
        [AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
            "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
            "recommended_action": "x", "rationale": "unexplained after retry",
        }),), stop_reason="tool_use")],
        raise_rate_limit_on_call=1,
    )
    trace = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", **_dirs(tmp_path))
    assert trace.final_decision == "exception"
    assert client.call_count == 2  # first call raised, second (retried) succeeded


def test_caching_avoids_a_second_llm_call_for_the_same_record(tmp_path):
    dirs = _dirs(tmp_path)
    client = FakeClient([
        AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
            "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
            "recommended_action": "x", "rationale": "cached case",
        }),), stop_reason="tool_use"),
    ])
    first = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", **dirs)
    assert not first.from_cache
    assert client.call_count == 1

    # A second run against the same record/model with an EMPTY turn queue -
    # if this reaches the client at all, FakeClient raises AssertionError.
    second = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", **dirs)
    assert second.from_cache
    assert client.call_count == 1  # unchanged - no new LLM call was made
    assert second.final_decision == "exception"


def test_changing_max_turns_misses_cache(tmp_path):
    # A record that hit AGENT_INCOMPLETE at the old budget must actually be
    # re-run at a new budget, not silently served the stale incomplete trace -
    # max_turns has to be part of the cache key, not just PROMPT_VERSION.
    dirs = _dirs(tmp_path)
    client = FakeClient([
        AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
            "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
            "recommended_action": "x", "rationale": "budget-8 case",
        }),), stop_reason="tool_use"),
    ])
    first = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", max_turns=8, **dirs)
    assert not first.from_cache
    assert client.call_count == 1

    client2 = FakeClient([
        AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
            "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
            "recommended_action": "x", "rationale": "budget-12 case",
        }),), stop_reason="tool_use"),
    ])
    second = run_agent_on_record("pay_1", "payments", _dataset(), client2, model_name="fake", backend_name="fake", max_turns=12, **dirs)
    assert not second.from_cache  # different max_turns must miss, not reuse the max_turns=8 cache entry
    assert client2.call_count == 1


def test_trace_file_is_persisted_with_the_record_id_as_filename(tmp_path):
    dirs = _dirs(tmp_path)
    client = FakeClient([
        AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
            "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
            "recommended_action": "x", "rationale": "persisted trace check",
        }),), stop_reason="tool_use"),
    ])
    run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", **dirs)
    trace_file = dirs["traces_dir"] / "pay_1.json"
    assert trace_file.exists()
    import json
    content = json.loads(trace_file.read_text())
    assert content["record_id"] == "pay_1"
    assert content["turns"][0]["tool_calls"][0]["name"] == "raise_exception"


def test_run_l3_aggregates_matches_and_exceptions_across_records(tmp_path):
    dirs = _dirs(tmp_path)
    # Scripted PER RECORD, not as one shared queue: run_l3 runs these two records
    # concurrently, so a single queue makes "which record gets which turn" a race.
    client = FakeClient(turns_by_record={
        "pay_1": [
            AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
                "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
                "recommended_action": "x", "rationale": "record one",
            }),), stop_reason="tool_use"),
        ],
        "btxn_1": [
            AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_2", name="get_record", arguments={"source": "settlements", "record_id": "setl_1"}),), stop_reason="tool_use"),
            AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_3", name="propose_match", arguments={
                "record_ids": ["btxn_1", "setl_1"], "confidence": 0.9, "rationale": "setl_1 matches btxn_1",
            }),), stop_reason="tool_use"),
        ],
    })
    output = run_l3(_dataset(), [("pay_1", "payments"), ("btxn_1", "bank")], client, model_name="fake", backend_name="fake", **dirs)
    # 1 exception from record pay_1, plus a HIGH_VALUE_MATCH_REVIEW companion
    # exception from btxn_1's match (its amount, Rs 1,00,000 at zero-MDR UPI,
    # exceeds the Rs 50,000 threshold) - both are real, expected outcomes.
    assert len(output.exceptions) == 2
    assert {e.category for e in output.exceptions} == {"UNEXPLAINED_VARIANCE", "HIGH_VALUE_MATCH_REVIEW"}
    assert len(output.matches) == 1
    assert output.meta.llm_calls == 3


# --- transient backend failures (found by a real 504 mid-run) -----------------

def test_transient_backend_error_is_retried(tmp_path, monkeypatch):
    """A 5xx/timeout means the request never produced a decision, so retrying is
    safe and correct. A real NIM 504 mid-batch is what motivated this."""
    import engine.l3_agent as l3_agent
    monkeypatch.setattr(l3_agent.time, "sleep", lambda s: None)

    class FlakyThenFine:
        def __init__(self):
            self.call_count = 0

        def complete(self, messages, tools):
            self.call_count += 1
            if self.call_count == 1:
                raise TransientBackendError("Error code: 504")
            return AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
                "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
                "recommended_action": "x", "rationale": "resolved after the 504 retry",
            }),), stop_reason="tool_use")

    client = FlakyThenFine()
    trace = run_agent_on_record("pay_1", "payments", _dataset(), client, model_name="fake", backend_name="fake", **_dirs(tmp_path))
    assert trace.final_decision == "exception"
    assert client.call_count == 2  # failed once, retried, succeeded


def test_one_records_failure_does_not_destroy_the_whole_batch(tmp_path, monkeypatch):
    """asyncio.gather propagates the first exception and discards every sibling
    result - so without isolation a single upstream failure on one record throws
    away every completed investigation in the batch."""
    import engine.l3_agent as l3_agent
    monkeypatch.setattr(l3_agent.time, "sleep", lambda s: None)

    class FailsOnlyForBtxn:
        def complete(self, messages, tools):
            text = " ".join(m.content or "" for m in messages if m.role == "user")
            if "btxn_1" in text:
                raise TransientBackendError("Error code: 504")  # never recovers
            return AssistantTurn(text=None, tool_calls=(ToolCall(id="tc_1", name="raise_exception", arguments={
                "category": "UNEXPLAINED_VARIANCE", "severity": "STANDARD", "amount_at_risk_paise": 1_00,
                "recommended_action": "x", "rationale": "this record succeeded",
            }),), stop_reason="tool_use")

    output = run_l3(
        _dataset(), [("pay_1", "payments"), ("btxn_1", "bank")], FailsOnlyForBtxn(),
        model_name="fake", backend_name="fake", **_dirs(tmp_path),
    )
    # the good record's result survives, and the failed one is visibly on the
    # ledger with its reason rather than silently vanishing
    categories = [e.category for e in output.exceptions]
    assert "UNEXPLAINED_VARIANCE" in categories, "the succeeding record's result was lost"
    assert "AGENT_INCOMPLETE" in categories, "the failed record vanished instead of being reported"
    failed = next(e for e in output.exceptions if e.category == "AGENT_INCOMPLETE")
    assert "504" in " ".join(failed.evidence_chain)
