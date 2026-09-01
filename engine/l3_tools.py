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
    SUGGESTED_OWNERS,
    Match,
    ReconException,
    severity_for_amount,
)
from .fees import UnknownFeeTier, compute_expected_fee
from .fees import explain_variance as _explain_variance
from .io import Dataset
from .io import aging_days as _aging_days
from .io import infer_as_of_date
from .l2_subset import Candidate, TooManyCandidates
from .l2_subset import solve_subset as _solve_subset
from .llm.base import ToolSpec

MIN_MATCH_CONFIDENCE = 0.85

# Two or more simultaneously-undecomposable comparisons means no single-cause
# hypothesis explains this record - definitionally UNEXPLAINED_VARIANCE, whatever
# category the model asks for. compound_fee_tax_error is exactly this: the fee
# tier and tax rate are both wrong, so explain_variance's fee-tier hypothesis
# (which assumes correct GST) and its GST-rate hypothesis (which assumes the
# correct fee) each fail by construction.
UNEXPLAINED_LEG_COERCION_THRESHOLD = 2

_ID_FIELD = {"orders": "order_id", "payments": "payment_id", "settlements": "settlement_id", "bank": "bank_txn_id"}
_DATE_FIELD = {"orders": "order_date", "payments": "captured_at", "settlements": "settled_at", "bank": "value_date"}
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


def _residual_aging_days(ctx: "ToolContext") -> int:
    """How long the record under investigation has been sitting, per its own
    underlying event (capture/settle/value date) - same convention and same
    "now" (infer_as_of_date) as engine/exceptions.py's deterministic
    exceptions, so the ledger reads consistently regardless of which layer
    raised a given row."""
    row = _find_record(ctx.dataset, ctx.residual_source, ctx.residual_id)
    date_field = _DATE_FIELD.get(ctx.residual_source)
    if row is None or date_field is None:
        return 0
    return _aging_days(infer_as_of_date(ctx.dataset), getattr(row, date_field))


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


@dataclass(frozen=True)
class VarianceObservation:
    """One explain_variance call and what it concluded. Recorded so
    raise_exception can see the shape of the investigation, not just its
    verdict."""

    observed_paise: int
    expected_paise: int
    delta_paise: int
    cause: str


@dataclass
class ToolContext:
    """Constructed once per agent invocation. `residual_id`/`residual_source`
    identify the one record this session is investigating - every propose_match
    links it to something else, every raise_exception is about it."""

    dataset: Dataset
    residual_id: str
    residual_source: str
    known_ids: set[str] = field(default_factory=set)
    variance_observations: list[VarianceObservation] = field(default_factory=list)
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
    # Recorded regardless of outcome so raise_exception can later see the full
    # shape of the investigation (see _unexplained_legs) - this call's own
    # return value is unchanged.
    ctx.variance_observations.append(VarianceObservation(
        observed_paise=observed_paise,
        expected_paise=expected_paise,
        delta_paise=explanation.delta_paise,
        cause=explanation.cause,
    ))
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

def _infer_link_type(dataset: Dataset, id_a: str, id_b: str) -> str | None:
    table_a, table_b = _table_for_id(id_a), _table_for_id(id_b)
    if table_a is None or table_b is None or table_a == table_b:
        return None
    link_type = _TABLE_PAIR_TO_LINK_TYPE.get(frozenset({table_a, table_b}))
    if link_type == "chargeback_payment":
        # A (payment, bank) pair is only ever a chargeback_payment link when the
        # bank row is a genuine debit. A payment's own settlement credit is also
        # a (payment, bank) pair by table alone - asserting chargeback_payment
        # for that would be a false link (caught in the live NIM run: the model
        # correctly traced payment -> settlement -> settlement's own bank
        # credit, and bundling all three ids into one propose_match call made
        # this code mislabel the third leg as a chargeback that never happened).
        bank_id = id_a if table_a == "bank" else id_b
        bank_row = _find_record(dataset, "bank", bank_id)
        if bank_row is None or bank_row.debit_paise <= 0:
            return None
    return link_type


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
        link_type = _infer_link_type(ctx.dataset, ctx.residual_id, other_id)
        if link_type is None:
            raise ToolError(
                f"cannot infer a valid link type between {ctx.residual_id!r} and {other_id!r} - if you were trying "
                f"to also assert a link between two OTHER records (e.g. a settlement and its own bank credit), "
                f"drop it from this call: propose_match only asserts links to the record under investigation"
            )
        left_id, right_id = _order_ids_for_link(link_type, ctx.residual_id, other_id)
        matches.append(Match(layer="L3", link_type=link_type, left_id=left_id, right_id=right_id, confidence=confidence, evidence=(rationale,)))

    # Constraint 4: severity/review escalation is computed here, not trusted from the model.
    amount_at_risk = _amount_for(ctx, ctx.residual_id) or 0
    companion_exception = None
    if amount_at_risk > REVIEW_REQUIRED_THRESHOLD_PAISE:
        companion_affected = {"record_id": ctx.residual_id, "source": ctx.residual_source}
        if ctx.residual_source in _ID_FIELD:
            companion_affected[_ID_FIELD[ctx.residual_source]] = ctx.residual_id
        companion_exception = ReconException(
            category="HIGH_VALUE_MATCH_REVIEW",
            severity="REVIEW_REQUIRED",
            amount_at_risk_paise=amount_at_risk,
            affected=companion_affected,
            recommended_action=RECOMMENDED_ACTIONS["HIGH_VALUE_MATCH_REVIEW"],
            aging_days=_residual_aging_days(ctx),
            suggested_owner=SUGGESTED_OWNERS["HIGH_VALUE_MATCH_REVIEW"],
            evidence_chain=(rationale,),
        )

    payload = {"matches": matches, "companion_exception": companion_exception}
    ctx.result = ("match", payload)
    return {"status": "ok", "matches": json_safe(matches), "companion_exception": json_safe(companion_exception)}


def _unexplained_legs(ctx: ToolContext) -> list[VarianceObservation]:
    """Distinct comparisons explain_variance could not decompose.

    Deduped on (observed, expected) so a model retrying the identical call -
    which happens, and is legitimate - can never inflate the count into a
    false coercion.
    """
    seen: set[tuple[int, int]] = set()
    out: list[VarianceObservation] = []
    for obs in ctx.variance_observations:
        if obs.cause != "UNEXPLAINED" or obs.delta_paise == 0:
            continue
        key = (obs.observed_paise, obs.expected_paise)
        if key in seen:
            continue
        seen.add(key)
        out.append(obs)
    return out


def _bottom_line_leg(legs: list[VarianceObservation]) -> VarianceObservation:
    """The net-level comparison, identified as the one made against the largest
    expected amount: a net comparison is against the full net, a fee-leg
    comparison against the fee alone, typically two orders of magnitude smaller.
    This is the amount that actually moved the merchant's bank balance. When legs
    partially offset it is NOT the largest leg delta - a live run reporting a
    744-paise fee delta had a true net delta of 183 paise.

    Heuristic, not a proof: holds for the fee/GST/net shape this system produces,
    would break if two gross amounts were compared. Documented as an assumption.
    """
    return max(legs, key=lambda o: abs(o.expected_paise))


def raise_exception(ctx: ToolContext, category: str, severity: str, amount_at_risk_paise: int, recommended_action: str, rationale: str) -> dict:
    if ctx.result is not None:
        raise ToolError("a final decision was already made this session")
    # Never let the model invent its own taxonomy - a live run once returned
    # "FEE_CALCULATION_VARIANCE" and "unexplained_fee_variance", neither a
    # real category, which silently breaks category-keyed scoring
    # (compute_defect_confusion's exact-match check, the dashboard's
    # by-category breakdown) without ever raising an error. Rejecting here
    # gives the model a chance to retry with a real category within its
    # remaining turns, instead of polluting the exception ledger.
    if category not in RECOMMENDED_ACTIONS:
        raise ToolError(f"unknown category {category!r} - must be one of {sorted(RECOMMENDED_ACTIONS)}")
    if amount_at_risk_paise < 0:
        raise ToolError("amount_at_risk_paise must be non-negative")

    # Structural: if the model's own investigation showed two or more distinct
    # simultaneously-undecomposable comparisons, no single-cause category is
    # truthful, whichever one it asked for. Recompute category and amount from
    # the observations rather than trusting the request - same principle as
    # severity_for_amount below.
    legs = _unexplained_legs(ctx)
    multi_leg = len(legs) >= UNEXPLAINED_LEG_COERCION_THRESHOLD
    coerced_from = None
    leg_detail: tuple[str, ...] = ()
    if multi_leg:
        if category != "UNEXPLAINED_VARIANCE":
            coerced_from = category
            category = "UNEXPLAINED_VARIANCE"
        bottom_line = _bottom_line_leg(legs)
        amount_at_risk_paise = abs(bottom_line.delta_paise)
        leg_detail = tuple(
            f"unexplained leg: observed {o.observed_paise} vs expected "
            f"{o.expected_paise}, delta {o.delta_paise} paise"
            f"{' (bottom line)' if o is bottom_line else ''}"
            for o in legs
        )

    # Constraint 4: severity is recomputed from the (possibly just-recomputed)
    # amount, never trusted from the model.
    real_severity = severity_for_amount(amount_at_risk_paise)
    # affected carries BOTH the generic record_id/source (every consumer can
    # rely on these regardless of category) AND the source-specific id field
    # (payment_id/bank_txn_id/etc, matching engine/exceptions.py's own
    # convention). Without the latter, eval/metrics.py's CATEGORY_IDENTITY_FIELDS
    # lookup for e.g. FEE_VARIANCE (which expects "payment_id") never finds a
    # match against an L3 exception - a live run once made every non-
    # UNEXPLAINED_VARIANCE category L3 raised invisible to per-defect-class
    # scoring, silently, with no error.
    affected = {"record_id": ctx.residual_id, "source": ctx.residual_source}
    if ctx.residual_source in _ID_FIELD:
        affected[_ID_FIELD[ctx.residual_source]] = ctx.residual_id

    evidence: tuple[str, ...] = (rationale,) if rationale else ()
    if not evidence:
        raise ToolError("rationale must not be empty - every exception needs a reason on the ledger")
    if multi_leg:
        if coerced_from is not None:
            evidence += (
                f"category coerced from {coerced_from} to UNEXPLAINED_VARIANCE: "
                f"{len(legs)} distinct comparisons were simultaneously undecomposable, "
                f"so no single-cause category applies",
            )
        evidence += leg_detail

    exc = ReconException(
        category=category,
        severity=real_severity,
        amount_at_risk_paise=amount_at_risk_paise,
        affected=affected,
        recommended_action=recommended_action or RECOMMENDED_ACTIONS.get(category, "Manual review required."),
        aging_days=_residual_aging_days(ctx),
        suggested_owner=SUGGESTED_OWNERS.get(category, "Reconciliation Ops"),
        evidence_chain=evidence,
    )

    ctx.result = ("exception", {"exception": exc})
    return {
        "status": "ok",
        "exception": json_safe(exc),
        "category_coerced_from": coerced_from,
        "unexplained_leg_count": len(legs),
    }


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
    ToolSpec("propose_match", (
        "Assert that the record under investigation links to one or more other known records. "
        "Every id in record_ids must relate DIRECTLY to the record under investigation - do not "
        "include a record that only relates to one of the OTHER ids you're proposing (e.g. a "
        "settlement's own bank credit, when you are investigating the payment, not the settlement). "
        "Requires confidence >= 0.85."
    ), {
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
            "category": {
                "type": "string",
                "enum": sorted(RECOMMENDED_ACTIONS),
                "description": (
                    "If two or more separate comparisons each came back UNEXPLAINED, this is "
                    "UNEXPLAINED_VARIANCE - not the category of whichever single leg was "
                    "largest. Multiple simultaneously-undecomposable legs mean no single-cause "
                    "category is true, and this will be corrected automatically if you name one anyway."
                ),
            },
            "severity": {"type": "string"},
            "amount_at_risk_paise": {
                "type": "integer",
                "description": (
                    "The specific unexplained amount in question (e.g. the variance explain_variance "
                    "could not decompose) - NOT the payment's or settlement's full gross/net amount. "
                    "If the fee is off by 90 paise, this is 90, even if the payment itself is for "
                    "1,000,000 paise."
                ),
            },
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
