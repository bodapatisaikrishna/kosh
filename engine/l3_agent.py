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

from .contract import EngineMeta, EngineOutput, Match, ReconException
from .io import Dataset
from .l3_tools import TOOL_SPECS, ToolContext, _find_record, dispatch_tool, json_safe
from .llm.base import AssistantTurn, LLMClient, Message, RateLimitedError

PROMPT_VERSION = 1
DEFAULT_MAX_TURNS = 8
DEFAULT_CONCURRENCY = 8
MAX_BACKOFF_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0

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


def _cache_key(record_id: str, dataset: Dataset, source: str, model: str) -> str:
    """A snapshot of just this record's own fields, not the whole dataset - an
    unrelated dataset change doesn't invalidate every cached trace."""
    row = _find_record(dataset, source, record_id)
    snapshot = json_safe(row) if row is not None else None
    payload = json.dumps(
        {"record_id": record_id, "source": source, "model": model, "prompt_version": PROMPT_VERSION, "snapshot": snapshot},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _call_with_backoff(client: LLMClient, messages: list[Message], tools) -> AssistantTurn:
    delay = BASE_BACKOFF_SECONDS
    for attempt in range(MAX_BACKOFF_RETRIES):
        try:
            return client.complete(messages, tools)
        except RateLimitedError:
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
    cache_file = cache_dir / f"{_cache_key(record_id, dataset, source, model_name)}.json"
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
) -> EngineOutput:
    start = time.perf_counter()
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(record_id: str, source: str) -> AgentTrace:
        async with semaphore:
            return await asyncio.to_thread(
                run_agent_on_record, record_id, source, dataset, client, max_turns, cache_dir, traces_dir, model_name, backend_name,
            )

    traces = await asyncio.gather(*(run_one(rid, src) for rid, src in residual_items))
    elapsed = time.perf_counter() - start

    matches: list[Match] = []
    exceptions: list[ReconException] = []
    total_calls = total_input = total_output = 0
    for trace in traces:
        total_calls += trace.llm_calls
        total_input += trace.input_tokens
        total_output += trace.output_tokens
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
        meta=EngineMeta(wall_clock_seconds=elapsed, llm_calls=total_calls, input_tokens=total_input, output_tokens=total_output),
    )


def run_l3(dataset: Dataset, residual_items: list[tuple[str, str]], client: LLMClient, **kwargs) -> EngineOutput:
    """Sync wrapper for callers not already inside an event loop (the eval CLI,
    the pipeline's non-async entry points)."""
    return asyncio.run(run_l3_async(dataset, residual_items, client, **kwargs))
