"""L3: the agent loop over the fixed 7-tool set in engine/l3_tools.py.

Only the residual that survives L0-L2 matching and L4's deterministic
classification reaches here (see engine/pipeline.py). One agent invocation per
residual item, run concurrently under a semaphore, with exponential backoff on
a rate limit, a full trace persisted per record, and a content-hash cache so
repeated dev runs of an unchanged record are free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .contract import RECOMMENDED_ACTIONS, EngineMeta, EngineOutput, Match, ReconException, severity_for_amount
from .fees import compute_expected_fee
from .io import Dataset
from .io import aging_days as _aging_days
from .io import infer_as_of_date
from .l3_tools import _DATE_FIELD, _ID_FIELD, TOOL_SPECS, ToolContext, _find_record, dispatch_tool, json_safe
from .llm.base import AssistantTurn, LLMClient, Message, RateLimitedError, TransientBackendError
from .llm.pricing import cost_usd_micros

PROMPT_VERSION = 2  # bumped for Task 2's compound-leg category/amount coercion in l3_tools.raise_exception -
# a cache hit from PROMPT_VERSION=1 would silently replay a trace from before that fix existed.
DEFAULT_MAX_TURNS = 12
DEFAULT_CONCURRENCY = 8
MAX_BACKOFF_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0

# Hardening sprint, Task 4 finding: a live ablation run hung for 10+ hours
# with zero progress and no error, surviving a laptop sleep/wake cycle. Root
# cause: all DEFAULT_CONCURRENCY worker threads had a client.complete() call
# blocked on a TCP connection that went half-open across the sleep (8/8
# ESTABLISHED sockets, 0% CPU, no new traces) - the openai SDK's own 600s
# read timeout never fired, because nothing ever arrived to time out
# against; the socket just never signaled anything, forever. Per-record
# isolation (run_one's except Exception below) only protects the batch from
# an upstream call that eventually raises - it does nothing for one that
# never returns. This is the outer ceiling that makes it actually raise.
#
# Known residual gap, disclosed rather than hidden: asyncio.to_thread runs on
# the loop's default ThreadPoolExecutor, and a thread that is still
# physically blocked in a syscall when its wait_for times out keeps running
# in the background - Python cannot forcibly kill a thread. asyncio.run()'s
# own cleanup (shutdown_default_executor) joins every such thread before
# returning, so if one is blocked forever (not just past this ceiling), the
# call to run_l3()/run_llm_only() can still be slow to return even though
# every record's own result was already computed and persisted to
# traces_dir on time. This is a materially better failure mode than before
# the fix (zero results for 10+ hours vs. all-but-one record done and on
# disk promptly), not a complete guarantee against every hang.
PER_RECORD_TIMEOUT_SECONDS = 900.0

DEFAULT_TRACES_DIR = Path("traces")
DEFAULT_CACHE_DIR = Path("traces/.cache")

SYSTEM_PROMPT = """You are Kosh's reconciliation agent, investigating ONE record that \
deterministic matching and classification could not resolve.

Hard constraints - violating any of these is a failure:
1. You may only reference record IDs that a tool has returned to you in this session. Never construct an ID.
2. Never perform arithmetic yourself. Call compute_expected_fee or explain_variance. Your own mental math is not admissible evidence.
3. If your confidence is below 0.85, call raise_exception, not propose_match.
4. Any proposed match where the amount at risk exceeds Rs 50,000 gets severity=REVIEW_REQUIRED regardless of your confidence - this is enforced automatically, you don't need to set it yourself.
5. Every propose_match rationale must cite the specific tool outputs that support it.
6. You must end this investigation by calling either propose_match or raise_exception. Terminating without calling one of these is a failure. Always close the loop.
"""

REMINDER_MUST_CLOSE_LOOP = (
    "You have not yet called propose_match or raise_exception. You must close this "
    "investigation with one of those two tools before you finish."
)


@dataclass
class AgentTrace:
    record_id: str
    source: str
    model: str
    backend: str
    turns: list[dict] = field(default_factory=list)
    final_decision: str = "incomplete"  # "match" | "exception" | "incomplete"
    result: dict | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd_micros: int = 0
    from_cache: bool = False


def _amount_hint(dataset: Dataset, source: str, record_id: str) -> int:
    row = _find_record(dataset, source, record_id)
    if row is None:
        return 0
    if hasattr(row, "net_paise"):
        return row.net_paise
    if hasattr(row, "credit_paise"):
        return max(row.credit_paise, row.debit_paise)
    return 0


def _aging_days_hint(dataset: Dataset, source: str, record_id: str) -> int:
    """Same convention as l3_tools._residual_aging_days, for the upstream-
    failure fallback path that never reaches a ToolContext."""
    row = _find_record(dataset, source, record_id)
    date_field = _DATE_FIELD.get(source)
    if row is None or date_field is None:
        return 0
    event_date = getattr(row, date_field)
    if not event_date:
        # See l3_tools._residual_aging_days's identical comment: a "failed"
        # payment has no captured_at at all, and this is the fallback path
        # for exactly the kind of upstream failure that can be caused by one.
        return 0
    return _aging_days(infer_as_of_date(dataset), event_date)


def _cache_key(record_id: str, dataset: Dataset, source: str, model: str, max_turns: int) -> str:
    """A snapshot of just this record's own fields, not the whole dataset - an
    unrelated dataset change doesn't invalidate every cached trace.

    max_turns is part of the key deliberately: it changes what the agent is
    allowed to do (a record that hit AGENT_INCOMPLETE at max_turns=8 might
    close the loop cleanly at max_turns=12), so a budget change must miss
    cache the same way a prompt or model change does. Before this, only
    PROMPT_VERSION was in the key - changing max_turns without remembering
    to bump it would have silently served a stale trace from the old budget.
    """
    row = _find_record(dataset, source, record_id)
    snapshot = json_safe(row) if row is not None else None
    payload = json.dumps(
        {
            "record_id": record_id, "source": source, "model": model,
            "prompt_version": PROMPT_VERSION, "max_turns": max_turns, "snapshot": snapshot,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _call_with_backoff(client: LLMClient, messages: list[Message], tools) -> AssistantTurn:
    """Retries the two failure modes that are safe to retry: a rate limit ("slow
    down") and a transient backend error ("try again" - 5xx/timeout, where the
    request never produced a decision). Anything else propagates immediately,
    because retrying a genuine bad request just wastes budget."""
    delay = BASE_BACKOFF_SECONDS
    for attempt in range(MAX_BACKOFF_RETRIES):
        try:
            return client.complete(messages, tools)
        except (RateLimitedError, TransientBackendError):
            if attempt == MAX_BACKOFF_RETRIES - 1:
                raise
            time.sleep(delay + random.uniform(0, delay * 0.25))
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


def run_agent_on_record(
    record_id: str,
    source: str,
    dataset: Dataset,
    client: LLMClient,
    max_turns: int = DEFAULT_MAX_TURNS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    traces_dir: Path = DEFAULT_TRACES_DIR,
    model_name: str = "unknown",
    backend_name: str = "unknown",
) -> AgentTrace:
    cache_file = cache_dir / f"{_cache_key(record_id, dataset, source, model_name, max_turns)}.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        trace = AgentTrace(**{**cached, "llm_calls": 0, "from_cache": True})
        _persist_trace(traces_dir, trace)
        return trace

    ctx = ToolContext(dataset=dataset, residual_id=record_id, residual_source=source)
    messages = [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=(
            f"Investigate the {source} record {record_id}. It survived L0-L2 matching and "
            f"L4's deterministic classification unresolved."
        )),
    ]
    turns: list[dict] = []
    total_input_tokens = total_output_tokens = 0

    for turn_index in range(max_turns):
        assistant_turn = _call_with_backoff(client, messages, TOOL_SPECS)
        total_input_tokens += assistant_turn.input_tokens
        total_output_tokens += assistant_turn.output_tokens
        messages.append(Message(role="assistant", content=assistant_turn.text, tool_calls=assistant_turn.tool_calls))

        turn_record = {"assistant_text": assistant_turn.text, "tool_calls": []}

        if not assistant_turn.tool_calls:
            turns.append(turn_record)
            if turn_index < max_turns - 1:
                messages.append(Message(role="user", content=REMINDER_MUST_CLOSE_LOOP))
                continue
            break  # out of turns, never closed the loop

        for tc in assistant_turn.tool_calls:
            result = dispatch_tool(ctx, tc.name, tc.arguments)
            turn_record["tool_calls"].append({"name": tc.name, "arguments": tc.arguments, "result": result})
            messages.append(Message(role="tool", content=json.dumps(result), tool_call_id=tc.id))
        turns.append(turn_record)

        if ctx.result is not None:
            break

    if ctx.result is None:
        # Constraint 6, enforced at the infrastructure level: an investigation
        # that never closes the loop is never silently dropped - it becomes its
        # own honest exception instead.
        dispatch_tool(ctx, "raise_exception", {
            "category": "AGENT_INCOMPLETE",
            "severity": "STANDARD",
            "amount_at_risk_paise": _amount_hint(dataset, source, record_id),
            "recommended_action": "",  # falls back to RECOMMENDED_ACTIONS["AGENT_INCOMPLETE"]
            "rationale": f"agent loop exhausted {max_turns} turns without calling propose_match or raise_exception",
        })

    final_decision = ctx.result[0] if ctx.result else "incomplete"
    result_payload = json_safe(ctx.result[1]) if ctx.result else None

    trace = AgentTrace(
        record_id=record_id, source=source, model=model_name, backend=backend_name,
        turns=turns, final_decision=final_decision, result=result_payload,
        llm_calls=len(turns), input_tokens=total_input_tokens, output_tokens=total_output_tokens,
        cost_usd_micros=cost_usd_micros(model_name, total_input_tokens, total_output_tokens),
        from_cache=False,
    )
    _write_cache(cache_file, trace)
    _persist_trace(traces_dir, trace)
    return trace


def _write_cache(cache_file: Path, trace: AgentTrace) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in asdict(trace).items() if k != "from_cache"}
    cache_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _persist_trace(traces_dir: Path, trace: AgentTrace) -> None:
    traces_dir.mkdir(parents=True, exist_ok=True)
    (traces_dir / f"{trace.record_id}.json").write_text(json.dumps(asdict(trace), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _match_from_dict(d: dict) -> Match:
    return Match(layer=d["layer"], link_type=d["link_type"], left_id=d["left_id"], right_id=d["right_id"], confidence=d["confidence"], evidence=tuple(d.get("evidence", ())))


def _exception_from_dict(d: dict) -> ReconException:
    return ReconException(
        category=d["category"], severity=d["severity"], amount_at_risk_paise=d["amount_at_risk_paise"],
        affected=d["affected"], recommended_action=d["recommended_action"], aging_days=d.get("aging_days", 0),
        evidence_chain=tuple(d.get("evidence_chain", ())), suggested_owner=d.get("suggested_owner", "Reconciliation Ops"),
    )


async def run_l3_async(
    dataset: Dataset,
    residual_items: list[tuple[str, str]],
    client: LLMClient,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_turns: int = DEFAULT_MAX_TURNS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    traces_dir: Path = DEFAULT_TRACES_DIR,
    model_name: str = "unknown",
    backend_name: str = "unknown",
    per_record_timeout_seconds: float = PER_RECORD_TIMEOUT_SECONDS,
) -> EngineOutput:
    start = time.perf_counter()
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(record_id: str, source: str) -> AgentTrace:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        run_agent_on_record, record_id, source, dataset, client, max_turns, cache_dir, traces_dir, model_name, backend_name,
                    ),
                    timeout=per_record_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see below
                # One record's upstream failure must never destroy the whole
                # batch. asyncio.gather propagates the first exception and
                # discards every sibling result, so a single 504 on record 4
                # would throw away five completed investigations. Instead the
                # failure becomes its own honest ledger entry - the record is
                # visibly unresolved with the reason attached, which is the
                # same contract as constraint 6's AGENT_INCOMPLETE.
                #
                # asyncio.TimeoutError (raised by wait_for above, per the
                # PER_RECORD_TIMEOUT_SECONDS comment) is deliberately caught
                # here too, not just genuine backend errors: a to_thread call
                # that never returns is not "still working", it is a stuck
                # worker permanently holding this record's semaphore slot -
                # timing it out is what makes the other DEFAULT_CONCURRENCY-1
                # slots (and, once the semaphore releases, this one too) keep
                # making progress instead of the whole batch silently halting.
                if isinstance(exc, TimeoutError):
                    reason = f"no response from {backend_name} after {per_record_timeout_seconds:.0f}s (timed out)"
                else:
                    reason = f"agent run failed against {backend_name}: {type(exc).__name__}: {exc}"
                amount = _amount_hint(dataset, source, record_id)
                # See l3_tools.raise_exception's identical comment: the
                # source-specific id field (payment_id/etc) is required for
                # eval/metrics.py's CATEGORY_IDENTITY_FIELDS lookup, not just
                # the generic record_id/source pair.
                failed_affected = {"record_id": record_id, "source": source}
                if source in _ID_FIELD:
                    failed_affected[_ID_FIELD[source]] = record_id
                failed = AgentTrace(
                    record_id=record_id, source=source, model=model_name, backend=backend_name,
                    final_decision="exception",
                    result={"exception": json_safe(ReconException(
                        category="AGENT_INCOMPLETE",
                        severity=severity_for_amount(amount),
                        amount_at_risk_paise=amount,
                        affected=failed_affected,
                        recommended_action=RECOMMENDED_ACTIONS["AGENT_INCOMPLETE"],
                        aging_days=_aging_days_hint(dataset, source, record_id),
                        evidence_chain=(reason,),
                    ))},
                )
                _persist_trace(traces_dir, failed)
                return failed

    traces = await asyncio.gather(*(run_one(rid, src) for rid, src in residual_items))
    elapsed = time.perf_counter() - start

    matches: list[Match] = []
    exceptions: list[ReconException] = []
    total_calls = total_input = total_output = total_cost = 0
    for trace in traces:
        total_calls += trace.llm_calls
        total_input += trace.input_tokens
        total_output += trace.output_tokens
        total_cost += trace.cost_usd_micros
        if not trace.result:
            continue
        if trace.final_decision == "match":
            matches.extend(_match_from_dict(m) for m in trace.result.get("matches", []))
            companion = trace.result.get("companion_exception")
            if companion:
                exceptions.append(_exception_from_dict(companion))
        elif "exception" in trace.result:
            exceptions.append(_exception_from_dict(trace.result["exception"]))

    return EngineOutput(
        matches=matches, exceptions=exceptions,
        meta=EngineMeta(
            wall_clock_seconds=elapsed, llm_calls=total_calls,
            input_tokens=total_input, output_tokens=total_output, cost_usd_micros=total_cost,
        ),
    )


def run_l3(dataset: Dataset, residual_items: list[tuple[str, str]], client: LLMClient, **kwargs) -> EngineOutput:
    """Sync wrapper for callers not already inside an event loop (the eval CLI,
    the pipeline's non-async entry points)."""
    return asyncio.run(run_l3_async(dataset, residual_items, client, **kwargs))


def _synthetic_exercise_dataset():
    """A small, hand-built dataset covering every hard constraint and every
    tool at least once - NOT run_2000 residual (which is empty; see
    ARCHITECTURE.md). Five records, each engineered to need a different kind
    of investigation:

      pay_unexplained  - net doesn't fit any deterministic hypothesis
      pay_high_value   - a clean, high-confidence, >Rs 50,000 match
      pay_weak_evidence - only a loosely-plausible candidate exists
      btxn_batch       - a bank credit that is a genuine 2-settlement batch
      pay_ambiguous    - two equally plausible settlement candidates
    """
    from .io import BankRow, Dataset, OrderRow, PaymentRow, SettlementRow

    orders, payments, settlements, bank = [], [], [], []

    # 1. pay_unexplained: booked net is off by an arbitrary amount.
    fee, gst, correct_net = compute_expected_fee(200_000_00, "upi", False)
    orders.append(OrderRow("order_unexplained", "2026-08-01", "cust_1", 200_000_00, "INR", "upi", "paid", "INV-1"))
    payments.append(PaymentRow("pay_unexplained", "order_unexplained", "2026-08-01T10:00:00", "upi", False,
                                200_000_00, fee, gst, correct_net - 7_777, "captured", "setl_unexplained", None, 0))
    settlements.append(SettlementRow("setl_unexplained", "2026-08-03", "HDFCN00000000101", 1, 200_000_00, fee, gst, 0, correct_net - 7_777))
    bank.append(BankRow("btxn_unexplained", "2026-08-03", "NEFT-HDFCN00000000101-RAZORPAY SOFTWARE PVT LTD", correct_net - 7_777, 0, 0))

    # 2. pay_high_value: clean candidate, gross > Rs 50,000 net at zero-MDR UPI.
    fee2, gst2, net2 = compute_expected_fee(600_000_00, "upi", False)
    orders.append(OrderRow("order_high_value", "2026-08-05", "cust_2", 600_000_00, "INR", "upi", "paid", "INV-2"))
    payments.append(PaymentRow("pay_high_value", "order_high_value", "2026-08-05T11:00:00", "upi", False,
                                600_000_00, fee2, gst2, net2, "captured", None, None, 0))
    settlements.append(SettlementRow("setl_high_value", "2026-08-07", "ICICN00000000102", 1, 600_000_00, fee2, gst2, 0, net2))
    bank.append(BankRow("btxn_high_value", "2026-08-07", "NEFT-ICICN00000000102-RAZORPAY SOFTWARE PVT LTD", net2, 0, 0))

    # 3. pay_weak_evidence: a same-day settlement exists whose net is close
    # (within ~Rs 500) but not exact, AND it aggregates 3 payments, not 1 - a
    # genuine "this could plausibly be it, but the evidence doesn't cleanly
    # support a single-payment match" case.
    fee3, gst3, net3 = compute_expected_fee(80_000_00, "card", False)
    orders.append(OrderRow("order_weak", "2026-08-10", "cust_3", 80_000_00, "INR", "card", "paid", "INV-3"))
    payments.append(PaymentRow("pay_weak_evidence", "order_weak", "2026-08-10T09:00:00", "card", False,
                                80_000_00, fee3, gst3, net3, "captured", None, None, 0))
    settlements.append(SettlementRow("setl_weak_maybe", "2026-08-12", "UTIBN00000000103", 3, 240_000_00, 4_800_00, 864_00, 0, net3 + 500_00))
    bank.append(BankRow("btxn_weak_maybe", "2026-08-12", "NEFT-UTIBN00000000103-RAZORPAY SOFTWARE PVT LTD", net3 + 500_00, 0, 0))

    # 4. btxn_batch: one bank credit that is the sum of two settlements neither
    # of which alone matches it - a genuine multi-settlement batch.
    fee4a, gst4a, net4a = compute_expected_fee(150_000_00, "upi", False)
    fee4b, gst4b, net4b = compute_expected_fee(250_000_00, "upi", False)
    settlements.append(SettlementRow("setl_batch_a", "2026-08-15", "KKBKN00000000104", 1, 150_000_00, fee4a, gst4a, 0, net4a))
    settlements.append(SettlementRow("setl_batch_b", "2026-08-15", "SBINN00000000105", 1, 250_000_00, fee4b, gst4b, 0, net4b))
    batch_total = net4a + net4b
    bank.append(BankRow("btxn_batch", "2026-08-15", "BATCH SETTLEMENT CREDIT - MULTIPLE REFERENCES", batch_total, 0, 0))

    # 5. pay_ambiguous: two settlements equally plausible (same amount, same date).
    fee5, gst5, net5 = compute_expected_fee(45_000_00, "netbanking", False)
    orders.append(OrderRow("order_ambiguous", "2026-08-20", "cust_5", 45_000_00, "INR", "netbanking", "paid", "INV-5"))
    payments.append(PaymentRow("pay_ambiguous", "order_ambiguous", "2026-08-20T14:00:00", "netbanking", False,
                                45_000_00, fee5, gst5, net5, "captured", None, None, 0))
    settlements.append(SettlementRow("setl_ambiguous_a", "2026-08-22", "HDFCN00000000106", 1, 45_000_00, fee5, gst5, 0, net5))
    settlements.append(SettlementRow("setl_ambiguous_b", "2026-08-22", "ICICN00000000107", 1, 45_000_00, fee5, gst5, 0, net5))
    bank.append(BankRow("btxn_ambiguous_a", "2026-08-22", "NEFT-HDFCN00000000106-RAZORPAY SOFTWARE PVT LTD", net5, 0, 0))
    bank.append(BankRow("btxn_ambiguous_b", "2026-08-22", "NEFT-ICICN00000000107-RAZORPAY SOFTWARE PVT LTD", net5, 0, 0))

    return Dataset(orders=orders, payments=payments, settlements=settlements, bank=bank)


SYNTHETIC_RESIDUAL_ITEMS: list[tuple[str, str]] = [
    ("pay_unexplained", "payments"),
    ("pay_high_value", "payments"),
    ("pay_weak_evidence", "payments"),
    ("btxn_batch", "bank"),
    ("pay_ambiguous", "payments"),
]


def _profile(backend: str, model: str | None, out_path: str | None, traces_dir: str, cache_dir: str) -> None:
    """`python -m engine.l3_agent --profile [--backend nim|anthropic] [--model ID]
    [--out path.json] [--traces-dir DIR] [--cache-dir DIR]`

    Runs the fixed 5-record synthetic exercise set (NOT run_2000 residual, which
    is empty - see ARCHITECTURE.md) against a real backend, persists a trace per
    record, and reports the same throughput/behavior shape as a benchmark JSON.
    """
    if backend == "nim":
        from .llm.nim_client import NimClient
        client = NimClient(model=model) if model else NimClient()
        model_name, backend_name = client._model, "nim"
    elif backend == "anthropic":
        from .llm.anthropic_client import AnthropicClient
        client = AnthropicClient(model=model) if model else AnthropicClient()
        model_name, backend_name = client._model, "anthropic"
    else:
        raise SystemExit(f"unknown backend {backend!r}")

    dataset = _synthetic_exercise_dataset()
    output = run_l3(
        dataset, SYNTHETIC_RESIDUAL_ITEMS, client,
        concurrency=len(SYNTHETIC_RESIDUAL_ITEMS),
        cache_dir=Path(cache_dir), traces_dir=Path(traces_dir),
        model_name=model_name, backend_name=backend_name,
    )

    report = {
        "note": "SYNTHETIC exercise set, hand-built to exercise every hard constraint and tool at "
                "least once against a real LLM backend - complements benchmarks/phase5_live_residual.json, "
                "which is the real run_2000 residual (non-empty after dataset hardening; see ARCHITECTURE.md).",
        "backend": backend_name,
        "model": model_name,
        "records": len(SYNTHETIC_RESIDUAL_ITEMS),
        "wall_clock_seconds": output.meta.wall_clock_seconds,
        "llm_calls": output.meta.llm_calls,
        "input_tokens": output.meta.input_tokens,
        "output_tokens": output.meta.output_tokens,
        "cost_usd_micros": output.meta.cost_usd_micros,
        "matches": [asdict(m) for m in output.matches],
        "exceptions": [asdict(e) for e in output.exceptions],
    }
    print(json.dumps({k: v for k, v in report.items() if k not in ("matches", "exceptions")}, indent=2, sort_keys=True))
    print(f"matches: {len(output.matches)}, exceptions: {len(output.exceptions)}")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the L3 synthetic exercise set against a real LLM backend.")
    parser.add_argument("--profile", action="store_true", help="run the synthetic exercise set (the only supported mode)")
    parser.add_argument("--backend", choices=["nim", "anthropic"], default="nim")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--traces-dir", type=str, default="traces")
    parser.add_argument("--cache-dir", type=str, default="traces/.cache")
    args = parser.parse_args()
    if args.profile:
        _profile(args.backend, args.model, args.out, args.traces_dir, args.cache_dir)
    else:
        parser.print_help()
