"""Strict public authorization snapshot schemas and safe policy projection."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.intent_audit import PublicIntentAssessment
from app.schemas.policy import DecisionOutcome, PolicyDecision, RuleStatus
from app.schemas.proposal import ProposalStatus

PolicyRuleId = Literal[
    "P01", "P02", "P03", "P04", "P05", "P06",
    "P07", "P08", "P09", "P10", "P11", "P12",
]

_RULE_CHECKS = {
    "P01": "Checks mandate signature integrity.",
    "P02": "Checks that the mandate is active and unexpired.",
    "P03": "Checks the mandate's total budget limit.",
    "P04": "Checks the per-item price limit.",
    "P05": "Checks whether the merchant is eligible.",
    "P06": "Checks whether the product category is eligible.",
    "P07": "Checks item quantity limits.",
    "P08": "Checks currency consistency.",
    "P09": "Checks live-price policy requirements.",
    "P10": "Checks the daily spend limit.",
    "P11": "Checks for duplicate or replayed proposals.",
    "P12": "Checks proposal data integrity.",
}


class PublicPolicySeverity(str, Enum):
    """Customer-facing severity derived from a deterministic rule outcome."""

    INFORMATIONAL = "INFORMATIONAL"
    REVIEW = "REVIEW"
    BLOCKING = "BLOCKING"


def public_policy_explanation(rule_id: str, outcome: RuleStatus) -> str:
    """Build fixed customer-safe text without using internal rule evidence or reasons."""
    outcome_text = {
        RuleStatus.PASS: "This check passed.",
        RuleStatus.BLOCK: "This check blocks the proposal.",
        RuleStatus.REQUIRE_APPROVAL: "This check requires separate user review.",
        RuleStatus.NOT_APPLICABLE: "This check does not apply to this proposal.",
    }[outcome]
    return f"{_RULE_CHECKS[rule_id]} {outcome_text}"


def _severity_for(outcome: RuleStatus) -> PublicPolicySeverity:
    if outcome == RuleStatus.BLOCK:
        return PublicPolicySeverity.BLOCKING
    if outcome == RuleStatus.REQUIRE_APPROVAL:
        return PublicPolicySeverity.REVIEW
    return PublicPolicySeverity.INFORMATIONAL


class PublicPolicyRule(BaseModel):
    """Allowlisted policy rule details safe for customer-facing use."""

    model_config = ConfigDict(extra="forbid")

    rule_id: PolicyRuleId
    outcome: RuleStatus
    severity: PublicPolicySeverity
    explanation: str = Field(min_length=1, max_length=256)
    requires_user_review: bool

    @model_validator(mode="after")
    def validate_derived_fields(self) -> "PublicPolicyRule":
        """Require all public text and classifications to match fixed allowlists."""
        if self.severity != _severity_for(self.outcome):
            raise ValueError("Public rule severity does not match its outcome.")
        if self.requires_user_review != (self.outcome == RuleStatus.REQUIRE_APPROVAL):
            raise ValueError("Public review flag does not match its outcome.")
        if self.explanation != public_policy_explanation(self.rule_id, self.outcome):
            raise ValueError("Public rule explanation is not an approved safe explanation.")
        return self


class PublicPolicyDecision(BaseModel):
    """Minimal deterministic policy result for an authorization snapshot."""

    model_config = ConfigDict(extra="forbid")

    decision: DecisionOutcome
    rules: list[PublicPolicyRule] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def validate_rule_set_and_outcome(self) -> "PublicPolicyDecision":
        rule_ids = [rule.rule_id for rule in self.rules]
        expected = {f"P{index:02d}" for index in range(1, 13)}
        if len(set(rule_ids)) != 12 or set(rule_ids) != expected:
            raise ValueError("Public policy must contain each deterministic rule exactly once.")

        outcomes = {rule.outcome for rule in self.rules}
        if RuleStatus.BLOCK in outcomes:
            if self.decision != DecisionOutcome.BLOCK:
                raise ValueError("A blocking rule requires an aggregate BLOCK decision.")
        elif RuleStatus.REQUIRE_APPROVAL in outcomes:
            if self.decision != DecisionOutcome.REQUIRE_APPROVAL:
                raise ValueError("A review rule requires an aggregate review decision.")
        elif self.decision == DecisionOutcome.BLOCK:
            raise ValueError("An aggregate BLOCK requires a blocking rule.")
        return self


def public_policy_decision(decision: PolicyDecision) -> PublicPolicyDecision:
    """Project the full internal decision to a fixed, evidence-free public contract."""
    rules = [
        PublicPolicyRule(
            rule_id=rule.rule_id,
            outcome=rule.status,
            severity=_severity_for(rule.status),
            explanation=public_policy_explanation(rule.rule_id, rule.status),
            requires_user_review=rule.status == RuleStatus.REQUIRE_APPROVAL,
        )
        for rule in decision.rules
    ]
    return PublicPolicyDecision(decision=decision.decision, rules=rules)


class AuthorizationSnapshotResponse(BaseModel):
    """Policy, lifecycle, and auditor state read from one database snapshot."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    proposal_status: ProposalStatus
    policy_decision: PublicPolicyDecision
    assessment: PublicIntentAssessment | None
