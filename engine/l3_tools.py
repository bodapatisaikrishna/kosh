"""The 7 tools exposed to the L3 agent, and the structural enforcement of the
hard constraints from the system prompt. The prompt asks nicely; this module is
what actually stops the model from doing something it shouldn't - an ID that
never came from a tool, a sub-0.85-confidence match, a severity it invented
instead of the one the amount actually requires.

Every tool takes a ToolContext as its first argument (constructed once per
agent run, holding the dataset, the set of IDs seen so far, and the eventual
final decision) and either returns a plain dict or raises ToolError - which the
agent loop turns into a tool result the model can see and react to, never a
silently dropped call.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime

from .contract import (
    LINK_TYPES,
    RECOMMENDED_ACTIONS,
    REVIEW_REQUIRED_THRESHOLD_PAISE,
    Match,
    ReconException,
    severity_for_amount,
)
from .fees import UnknownFeeTier, compute_expected_fee
from .fees import explain_variance as _explain_variance
from .io import Dataset
from .l2_subset import Candidate, TooManyCandidates
from .l2_subset import solve_subset as _solve_subset
from .llm.base import ToolSpec

MIN_MATCH_CONFIDENCE = 0.85

_ID_FIELD = {"orders": "order_id", "payments": "payment_id", "settlements": "settlement_id", "bank": "bank_txn_id"}
_PREFIX_TO_TABLE = {"order_": "orders", "pay_": "payments", "setl_": "settlements", "btxn_": "bank"}
_FIELD_TO_TABLE = {"order_id": "orders", "payment_id": "payments", "settlement_id": "settlements", "bank_txn_id": "bank"}
_TABLE_PAIR_TO_LINK_TYPE = {
    frozenset({"orders", "payments"}): "order_payment",
    frozenset({"payments", "settlements"}): "payment_settlement",
    frozenset({"settlements", "bank"}): "settlement_bank_txn",
    frozenset({"payments", "bank"}): "chargeback_payment",
}


class ToolError(Exception):
    """A tool's rejection of a call - the agent loop reports this back to the
    model as a tool result, it never silently ignores or drops the call."""


def _table_for_id(record_id: str) -> str | None:
    for prefix, table in _PREFIX_TO_TABLE.items():
        if record_id.startswith(prefix):
            return table
    return None


def _find_record(dataset: Dataset, table: str, record_id: str):
    id_field = _ID_FIELD[table]
    for row in getattr(dataset, table):
        if getattr(row, id_field) == record_id:
            return row
    return None


def json_safe(obj):
    """Recursively converts dataclasses/tuples into plain dicts/lists for
    json.dumps - used both for tool results sent back to the model and for
    trace persistence."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: json_safe(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    return obj


@dataclass
class ToolContext:
    """Constructed once per agent invocation. `residual_id`/`residual_source`
    identify the one record this session is investigating - every propose_match
    links it to something else, every raise_exception is about it."""

    dataset: Dataset
    residual_id: str
    residual_source: str
    known_ids: set[str] = field(default_factory=set)
    result: tuple[str, dict] | None = None  # ("match", {...}) | ("exception", {...})

    def __post_init__(self) -> None:
        self.known_ids.add(self.residual_id)


# --- get_record --------------------------------------------------------------

def get_record(ctx: ToolContext, source: str, record_id: str) -> dict:
    if source not in _ID_FIELD:
        raise ToolError(f"unknown source {source!r} - must be one of {sorted(_ID_FIELD)}")
    row = _find_record(ctx.dataset, source, record_id)
    if row is None:
        raise ToolError(f"no {source} record with id {record_id!r} - IDs must never be constructed, only looked up")
    ctx.known_ids.add(record_id)
    return json_safe(row)


# --- find_candidates -----------------------------------------------------------

def find_candidates(ctx: ToolContext, record_id: str, amount_tolerance_paise: int, date_window_days: int, limit: int = 10) -> list[dict]:
    if record_id not in ctx.known_ids:
        raise ToolError(f"{record_id!r} was not returned by a prior tool call - call get_record first")
    table = _table_for_id(record_id)
    row = _find_record(ctx.dataset, table, record_id) if table else None
    if row is None:
        raise ToolError(f"cannot find_candidates for unknown record {record_id!r}")

    found: list[tuple[str, object]] = []
    if table == "payments":
        anchor_date = datetime.fromisoformat(row.captured_at).date()
        for s in ctx.dataset.settlements:
            if abs(s.net_paise - row.net_paise) <= amount_tolerance_paise and abs((date.fromisoformat(s.settled_at) - anchor_date).days) <= date_window_days:
                found.append(("settlements", s))
    elif table == "bank":
        anchor_date = date.fromisoformat(row.value_date)
        if row.credit_paise > 0:
            for s in ctx.dataset.settlements:
                if abs(s.net_paise - row.credit_paise) <= amount_tolerance_paise and abs((date.fromisoformat(s.settled_at) - anchor_date).days) <= date_window_days:
                    found.append(("settlements", s))
        else:
            for p in ctx.dataset.payments:
                if p.status == "captured" and abs(p.net_paise - row.debit_paise) <= amount_tolerance_paise and abs((datetime.fromisoformat(p.captured_at).date() - anchor_date).days) <= date_window_days:
                    found.append(("payments", p))
    elif table == "settlements":
        anchor_date = date.fromisoformat(row.settled_at)
        for t in ctx.dataset.bank:
            if t.credit_paise > 0 and abs(t.credit_paise - row.net_paise) <= amount_tolerance_paise and abs((date.fromisoformat(t.value_date) - anchor_date).days) <= date_window_days:
                found.append(("bank", t))
    else:
        raise ToolError(f"find_candidates does not support source table {table!r} (only payments, bank, settlements)")

    results = []
    for tbl, rec in found[:limit]:
        rid = getattr(rec, _ID_FIELD[tbl])
        ctx.known_ids.add(rid)
        results.append({"table": tbl, **json_safe(rec)})
    return results


# --- compute_expected_fee / explain_variance ----------------------------------

def compute_expected_fee_tool(ctx: ToolContext, gross_paise: int, method: str, international: bool) -> dict:
    try:
        fee, gst, net = compute_expected_fee(gross_paise, method, international)
    except UnknownFeeTier as exc:
        raise ToolError(str(exc)) from exc
    return {"fee_paise": fee, "gst_paise": gst, "net_paise": net}


def explain_variance_tool(ctx: ToolContext, observed_paise: int, expected_paise: int, context: dict | None = None) -> dict:
    context = context or {}
    explanation = _explain_variance(
        observed_paise, expected_paise,
        gross_paise=context.get("gross_paise"),
        method=context.get("method"),
        international=context.get("international", False),
        known_refund_paise=context.get("known_refund_paise"),
    )
    return {"cause": explanation.cause, "delta_paise": explanation.delta_paise, "detail": explanation.detail}


# --- solve_subset --------------------------------------------------------------

def solve_subset_tool(ctx: ToolContext, target_paise: int, candidate_ids: list[str], tolerance_paise: int = 100) -> dict:
    unknown = [cid for cid in candidate_ids if cid not in ctx.known_ids]
    if unknown:
        raise ToolError(f"candidate ids not yet returned by a prior tool call: {unknown}")
    candidates = []
    for cid in candidate_ids:
        table = _table_for_id(cid)
        row = _find_record(ctx.dataset, table, cid) if table else None
        if row is None or not hasattr(row, "net_paise"):
            raise ToolError(f"{cid!r} has no net_paise amount to use in a subset-sum (only payments/settlements are supported)")
        candidates.append(Candidate(id=cid, amount_paise=row.net_paise))
    try:
        result = _solve_subset(target_paise, candidates, tolerance_paise=tolerance_paise)
    except TooManyCandidates as exc:
        raise ToolError(str(exc)) from exc
    return {
        "status": result.status,
        "chosen_ids": list(result.chosen_ids),
        "achieved_paise": result.achieved_paise,
        "alternative_solutions": [list(s) for s in result.alternative_solutions],
    }


# --- propose_match / raise_exception (the two ways to close the loop) --------

def _infer_link_type(id_a: str, id_b: str) -> str | None:
    table_a, table_b = _table_for_id(id_a), _table_for_id(id_b)
    if table_a is None or table_b is None or table_a == table_b:
        return None
    return _TABLE_PAIR_TO_LINK_TYPE.get(frozenset({table_a, table_b}))


def _order_ids_for_link(link_type: str, a: str, b: str) -> tuple[str, str]:
    left_field, _right_field = LINK_TYPES[link_type]
    return (a, b) if _table_for_id(a) == _FIELD_TO_TABLE[left_field] else (b, a)


def _amount_for(ctx: ToolContext, record_id: str) -> int | None:
    table = _table_for_id(record_id)
    row = _find_record(ctx.dataset, table, record_id) if table else None
    if row is None:
        return None
    if hasattr(row, "net_paise"):
        return row.net_paise
    if hasattr(row, "credit_paise"):
        return max(row.credit_paise, row.debit_paise)
    return None


def propose_match(ctx: ToolContext, record_ids: list[str], confidence: float, rationale: str) -> dict:
    if ctx.result is not None:
        raise ToolError("a final decision was already made this session")

    # Constraint 1: never reference an id that wasn't handed to you by a tool.
    unknown = [rid for rid in record_ids if rid not in ctx.known_ids]
    if unknown:
        raise ToolError(f"record_ids not yet returned by a prior tool call: {unknown} - never construct an id")

    # Constraint 3: below-threshold confidence must become an exception, not a match.
    if confidence < MIN_MATCH_CONFIDENCE:
        raise ToolError(f"confidence {confidence} is below {MIN_MATCH_CONFIDENCE} - call raise_exception instead")

    # Constraint 5 (best-effort): the rationale must cite something a tool actually returned.
    if not any(rid in rationale for rid in ctx.known_ids):
        raise ToolError("rationale must cite at least one record id returned by a prior tool call")

    others = [rid for rid in record_ids if rid != ctx.residual_id]
    if not others:
        raise ToolError("record_ids must include at least one id other than the record under investigation")

    matches = []
    for other_id in others:
        link_type = _infer_link_type(ctx.residual_id, other_id)
        if link_type is None:
            raise ToolError(f"cannot infer a link type between {ctx.residual_id!r} and {other_id!r}")
        left_id, right_id = _order_ids_for_link(link_type, ctx.residual_id, other_id)
        matches.append(Match(layer="L3", link_type=link_type, left_id=left_id, right_id=right_id, confidence=confidence, evidence=(rationale,)))

    # Constraint 4: severity/review escalation is computed here, not trusted from the model.
    amount_at_risk = _amount_for(ctx, ctx.residual_id) or 0
    companion_exception = None
    if amount_at_risk > REVIEW_REQUIRED_THRESHOLD_PAISE:
        companion_exception = ReconException(
            category="HIGH_VALUE_MATCH_REVIEW",
            severity="REVIEW_REQUIRED",
            amount_at_risk_paise=amount_at_risk,
            affected={"record_id": ctx.residual_id},
            recommended_action=RECOMMENDED_ACTIONS["HIGH_VALUE_MATCH_REVIEW"],
            evidence_chain=(rationale,),
        )

    payload = {"matches": matches, "companion_exception": companion_exception}
    ctx.result = ("match", payload)
    return {"status": "ok", "matches": json_safe(matches), "companion_exception": json_safe(companion_exception)}


def raise_exception(ctx: ToolContext, category: str, severity: str, amount_at_risk_paise: int, recommended_action: str, rationale: str) -> dict:
    if ctx.result is not None:
        raise ToolError("a final decision was already made this session")
    if amount_at_risk_paise < 0:
        raise ToolError("amount_at_risk_paise must be non-negative")

    # Constraint 4: severity is recomputed from the amount, never trusted from the model.
    real_severity = severity_for_amount(amount_at_risk_paise)
    exc = ReconException(
        category=category,
        severity=real_severity,
        amount_at_risk_paise=amount_at_risk_paise,
        affected={"record_id": ctx.residual_id, "source": ctx.residual_source},
        recommended_action=recommended_action or RECOMMENDED_ACTIONS.get(category, "Manual review required."),
        evidence_chain=(rationale,) if rationale else (),
    )
    if not exc.evidence_chain:
        raise ToolError("rationale must not be empty - every exception needs a reason on the ledger")

    ctx.result = ("exception", {"exception": exc})
    return {"status": "ok", "exception": json_safe(exc)}


# --- tool specs + dispatch ----------------------------------------------------

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("get_record", "Fetch a record by source table and id.", {
        "type": "object",
        "properties": {"source": {"type": "string", "enum": ["orders", "payments", "settlements", "bank"]}, "record_id": {"type": "string"}},
        "required": ["source", "record_id"],
    }),
    ToolSpec("find_candidates", "Find candidate related records within an amount and date tolerance of a record you already know about.", {
        "type": "object",
        "properties": {
            "record_id": {"type": "string"},
            "amount_tolerance_paise": {"type": "integer"},
            "date_window_days": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["record_id", "amount_tolerance_paise", "date_window_days"],
    }),
    ToolSpec("compute_expected_fee", "Compute the expected fee, GST, and net for a gross amount, method, and international flag.", {
        "type": "object",
        "properties": {"gross_paise": {"type": "integer"}, "method": {"type": "string"}, "international": {"type": "boolean"}},
        "required": ["gross_paise", "method", "international"],
    }),
    ToolSpec("explain_variance", "Decompose an observed-vs-expected paise delta into a known cause (rounding, fee tier, GST rate, refund) or UNEXPLAINED.", {
        "type": "object",
        "properties": {
            "observed_paise": {"type": "integer"},
            "expected_paise": {"type": "integer"},
            "context": {"type": "object", "description": "optional: gross_paise, method, international, known_refund_paise"},
        },
        "required": ["observed_paise", "expected_paise"],
    }),
    ToolSpec("solve_subset", "Find a subset of known candidate ids whose net amounts sum to a target, within tolerance.", {
        "type": "object",
        "properties": {
            "target_paise": {"type": "integer"},
            "candidate_ids": {"type": "array", "items": {"type": "string"}},
            "tolerance_paise": {"type": "integer"},
        },
        "required": ["target_paise", "candidate_ids"],
    }),
    ToolSpec("propose_match", "Assert that the record under investigation links to one or more other known records. Requires confidence >= 0.85.", {
        "type": "object",
        "properties": {
            "record_ids": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "rationale": {"type": "string"},
        },
        "required": ["record_ids", "confidence", "rationale"],
    }),
    ToolSpec("raise_exception", "Close this investigation by raising an exception ledger entry for the record under investigation.", {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "severity": {"type": "string"},
            "amount_at_risk_paise": {"type": "integer"},
            "recommended_action": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["category", "severity", "amount_at_risk_paise", "recommended_action", "rationale"],
    }),
]

_DISPATCH = {
    "get_record": get_record,
    "find_candidates": find_candidates,
    "compute_expected_fee": compute_expected_fee_tool,
    "explain_variance": explain_variance_tool,
    "solve_subset": solve_subset_tool,
    "propose_match": propose_match,
    "raise_exception": raise_exception,
}


def dispatch_tool(ctx: ToolContext, name: str, arguments: dict) -> dict:
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"status": "error", "error": f"unknown tool {name!r}"}
    try:
        result = fn(ctx, **arguments)
    except ToolError as exc:
        return {"status": "error", "error": str(exc)}
    except TypeError as exc:
        return {"status": "error", "error": f"invalid arguments for {name}: {exc}"}
    if isinstance(result, dict) and "status" in result:
        return result
    return {"status": "ok", "result": json_safe(result)}
