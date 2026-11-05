"""Pydantic schemas and models for the LEASH Policy Engine."""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuleStatus(str, Enum):
    """Evaluation outcome for an individual policy rule."""

    PASS = "PASS"
    BLOCK = "BLOCK"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DecisionOutcome(str, Enum):
    """Aggregate policy authorization decision."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class RuleResult(BaseModel):
    """Structured evaluation finding from a single policy rule."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(..., description="Unique rule identifier (e.g. P01).")
    rule_name: str = Field(..., description="Human-readable rule name.")
    status: RuleStatus = Field(..., description="Rule evaluation outcome.")
    reason: str = Field(..., description="Deterministic explanation of the finding.")
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="Audit evidence supporting the evaluation."
    )
    evaluated_at: datetime = Field(..., description="Timestamp when the rule was evaluated.")

    @field_validator("evaluated_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime) -> datetime:
        """Ensure evaluated_at is timezone-aware UTC."""
        if v.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware UTC.")
        return v.astimezone(timezone.utc)


class OracleResult(BaseModel):
    """External live-price reading provided to the policy engine."""

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(..., description="Product identifier queried from oracle.")
    live_price: Decimal | None = Field(
        default=None, description="Current market unit price from oracle."
    )
    fetched_at: datetime = Field(..., description="Timestamp when oracle data was captured.")
    is_available: bool = Field(default=True, description="Whether oracle service was reachable.")
    is_stale: bool = Field(default=False, description="Whether oracle data exceeds TTL.")

    @field_validator("fetched_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime) -> datetime:
        """Ensure fetched_at is timezone-aware UTC."""
        if v.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware UTC.")
        return v.astimezone(timezone.utc)


class EvaluationContext(BaseModel):
    """Immutable context injected into policy evaluation."""

    model_config = ConfigDict(extra="forbid")

    now: datetime = Field(..., description="Authoritative UTC evaluation timestamp.")
    today_reserved: Decimal = Field(
        default=Decimal("0.00"), description="Total reserved spend today for mandate."
    )
    daily_txn_limit: Decimal | None = Field(
        default=None, description="Optional daily spend limit."
    )
    cumulative_mandate_spent: Decimal = Field(
        default=Decimal("0.00"), description="Total lifetime spend approved for mandate."
    )
    oracle_result: OracleResult | None = Field(
        default=None, description="Optional price oracle reading for P09."
    )

    @field_validator("now", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime) -> datetime:
        """Ensure now timestamp is timezone-aware UTC."""
        if v.tzinfo is None:
            raise ValueError("now must be timezone-aware UTC.")
        return v.astimezone(timezone.utc)


class PolicyDecision(BaseModel):
    """Aggregated policy decision for a purchase proposal."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(..., description="Evaluated proposal identifier.")
    mandate_id: str = Field(..., description="Associated mandate identifier.")
    decision: DecisionOutcome = Field(..., description="Overall authorization decision.")
    decision_at: datetime = Field(..., description="UTC timestamp of the decision.")
    rules: list[RuleResult] = Field(..., description="Evaluated rule results (all 12 rules).")
    blocking_rules: list[str] = Field(
        default_factory=list, description="IDs of rules that triggered BLOCK."
    )
    approval_required_rules: list[str] = Field(
        default_factory=list, description="IDs of rules requesting user approval."
    )
    is_one_time_exception: bool = Field(
        default=False, description="True if decision represents a one-time policy exception."
    )
    exception_types: list[str] = Field(
        default_factory=list, description="Categories of exceptions requested."
    )

    @field_validator("decision_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime) -> datetime:
        """Ensure decision_at is timezone-aware UTC."""
        if v.tzinfo is None:
            raise ValueError("decision_at must be timezone-aware UTC.")
        return v.astimezone(timezone.utc)


def aggregate_decision(
    proposal_id: str,
    mandate_id: str,
    rules: list[RuleResult],
    mandate_require_user_approval: bool,
    decision_at: datetime,
) -> PolicyDecision:
    """Aggregate individual rule findings into a final PolicyDecision.

    Follows strict precedence:
    1. If any rule is BLOCK -> outcome is BLOCK.
    2. Else if any rule is REQUIRE_APPROVAL or mandate_require_user_approval is True ->
       outcome is REQUIRE_APPROVAL.
    3. Else -> outcome is ALLOW.

    Args:
        proposal_id: The proposal under evaluation.
        mandate_id: The mandate being evaluated against.
        rules: Complete list of all evaluated RuleResult objects.
        mandate_require_user_approval: The mandate's require_user_approval setting.
        decision_at: Timestamp for the decision.

    Returns:
        Structured PolicyDecision instance.
    """
    blocking = [r.rule_id for r in rules if r.status == RuleStatus.BLOCK]
    req_approval = [r.rule_id for r in rules if r.status == RuleStatus.REQUIRE_APPROVAL]

    exception_types: list[str] = []
    for r in rules:
        if r.status == RuleStatus.REQUIRE_APPROVAL:
            if r.rule_id == "P04":
                exception_types.append("ITEM_PRICE_EXCEPTION")
            elif r.rule_id == "P05":
                exception_types.append("MERCHANT_EXCEPTION")
            elif r.rule_id == "P06":
                exception_types.append("CATEGORY_EXCEPTION")
            elif r.rule_id == "P07":
                exception_types.append("QUANTITY_EXCEPTION")
            elif r.rule_id == "P09":
                exception_types.append("PRICE_TOLERANCE_EXCEPTION")
            elif r.rule_id == "P10":
                exception_types.append("DAILY_LIMIT_EXCEPTION")

    if blocking:
        decision = DecisionOutcome.BLOCK
    elif req_approval or mandate_require_user_approval:
        decision = DecisionOutcome.REQUIRE_APPROVAL
    else:
        decision = DecisionOutcome.ALLOW

    is_one_time = bool(req_approval)

    return PolicyDecision(
        proposal_id=proposal_id,
        mandate_id=mandate_id,
        decision=decision,
        decision_at=decision_at,
        rules=rules,
        blocking_rules=blocking,
        approval_required_rules=req_approval,
        is_one_time_exception=is_one_time,
        exception_types=exception_types,
    )
