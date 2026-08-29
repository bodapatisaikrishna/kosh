"""The engine <-> eval contract.

Every layer built in Phases 3-5 (L0 deterministic, L1 tolerance, L2 subset-sum, L3
Claude agent) emits exactly this shape, and so do the two baseline "engines" in
baselines.py. The eval harness (eval/) only ever consumes an EngineOutput - it never
knows or cares whether the matches came from a regex join or an LLM tool call.

Money in Exception.amount_at_risk_paise is integer paise, per the project-wide rule.
LLM cost is integer micro-dollars (1 unit = $0.000001) for the same reason: never a
float in a field anyone will sum.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The three link types that appear in ground_truth.json["links"], and the ID-pair
# convention each one uses. Every Match must use one of these link_types, and its
# (left_id, right_id) must follow this order - it's how eval/metrics.py compares
# an asserted match against the true link graph.
LINK_TYPES: dict[str, tuple[str, str]] = {
    "order_payment": ("order_id", "payment_id"),
    "payment_settlement": ("payment_id", "settlement_id"),
    "settlement_bank_txn": ("settlement_id", "bank_txn_id"),
}

# Two severities for now; Phase 5's agent constraints introduce a stricter escalation
# rule (amount > INR 50,000 forces REVIEW_REQUIRED) that both baselines already honor.
SEVERITIES = ("STANDARD", "REVIEW_REQUIRED")
REVIEW_REQUIRED_THRESHOLD_PAISE = 50_000_00  # INR 50,000


@dataclass(frozen=True)
class Match:
    """One asserted link between two records."""

    layer: str  # "L0" | "L1" | "L2" | "L3" | "ORACLE" | "NULL"
    link_type: str  # a key of LINK_TYPES
    left_id: str
    right_id: str
    confidence: float
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.link_type not in LINK_TYPES:
            raise ValueError(f"unknown link_type {self.link_type!r}")


@dataclass(frozen=True)
class ReconException:
    """One item on the exception ledger. Named ReconException, not Exception, to
    avoid shadowing the builtin - this is a data record, never raised."""

    category: str
    severity: str
    amount_at_risk_paise: int
    affected: dict[str, str]
    recommended_action: str
    aging_days: int = 0

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")
        if self.amount_at_risk_paise < 0:
            raise ValueError("amount_at_risk_paise must be non-negative")


@dataclass
class EngineMeta:
    wall_clock_seconds: float
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd_micros: int = 0  # 1 unit = $0.000001; integer, never float


@dataclass
class EngineOutput:
    matches: list[Match] = field(default_factory=list)
    exceptions: list[ReconException] = field(default_factory=list)
    meta: EngineMeta = field(default_factory=lambda: EngineMeta(wall_clock_seconds=0.0))


def severity_for_amount(amount_at_risk_paise: int) -> str:
    """Phase 5's hard constraint #4 ("any proposed match/exception over INR 50,000
    gets severity=REVIEW_REQUIRED regardless of confidence") applies from Phase 2
    onward so severity assignment doesn't silently change once the real agent lands.
    """
    return "REVIEW_REQUIRED" if amount_at_risk_paise > REVIEW_REQUIRED_THRESHOLD_PAISE else "STANDARD"
