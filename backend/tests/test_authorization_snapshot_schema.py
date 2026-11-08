"""Tests for the explicit evidence-free public policy projection."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.authorization import (
    PublicPolicyRule,
    public_policy_decision,
    public_policy_explanation,
)
from app.schemas.policy import DecisionOutcome, PolicyDecision, RuleResult, RuleStatus
from app.services.intent_audit_service import policy_snapshot_hash


def _internal_decision() -> PolicyDecision:
    now = datetime.now(timezone.utc)
    rules = []
    for index in range(1, 13):
        rule_id = f"P{index:02d}"
        status = RuleStatus.REQUIRE_APPROVAL if rule_id == "P05" else RuleStatus.PASS
        rules.append(RuleResult(
            rule_id=rule_id,
            rule_name=f"Internal rule {index}",
            status=status,
            reason=f"Internal diagnostic for {rule_id}",
            evidence={
                "content_hash": "internal-content-hash-value",
                "fingerprint": "internal-fingerprint-value",
                "proposal_id": "internal-proposal-id-value",
                "mandate_id": "internal-mandate-id-value",
                "idempotency_key": "internal-idempotency-value",
                "product_source": "https://private.example/secret-path",
                "nested": {"future_sensitive_field": "never-public-value"},
            },
            evaluated_at=now,
        ))
    return PolicyDecision(
        proposal_id="pr_internal",
        mandate_id="man_internal",
        decision=DecisionOutcome.REQUIRE_APPROVAL,
        decision_at=now,
        rules=rules,
        approval_required_rules=["P05"],
        is_one_time_exception=True,
    )


def test_public_projection_preserves_outcomes_but_never_serializes_internal_evidence() -> None:
    internal = _internal_decision()
    before_hash = policy_snapshot_hash(internal)
    public = public_policy_decision(internal)
    serialized = public.model_dump_json()

    assert public.decision == internal.decision
    assert len(public.rules) == 12
    assert {rule.rule_id for rule in public.rules} == {
        f"P{index:02d}" for index in range(1, 13)
    }
    assert public.rules[4].outcome == RuleStatus.REQUIRE_APPROVAL
    assert public.rules[4].requires_user_review is True
    assert public.rules[4].severity.value == "REVIEW"
    for rule in public.rules:
        assert set(rule.model_dump()) == {
            "rule_id", "outcome", "severity", "explanation", "requires_user_review",
        }
    for private_value in (
        "internal-content-hash-value", "internal-fingerprint-value",
        "internal-proposal-id-value", "internal-mandate-id-value",
        "internal-idempotency-value", "private.example", "never-public-value",
    ):
        assert private_value not in serialized
    assert "evidence" not in serialized
    assert "Internal diagnostic" not in serialized

    # Projection does not mutate or replace the full decision required internally.
    assert internal.rules[0].evidence["content_hash"] == "internal-content-hash-value"
    assert policy_snapshot_hash(internal) == before_hash


def test_public_policy_rule_rejects_evidence_and_unapproved_explanations() -> None:
    base = {
        "rule_id": "P01",
        "outcome": RuleStatus.PASS,
        "severity": "INFORMATIONAL",
        "explanation": public_policy_explanation("P01", RuleStatus.PASS),
        "requires_user_review": False,
    }
    with pytest.raises(ValidationError):
        PublicPolicyRule.model_validate({**base, "evidence": {"future": "secret"}})
    with pytest.raises(ValidationError):
        PublicPolicyRule.model_validate({**base, "explanation": "Raw internal reason"})
    with pytest.raises(ValidationError):
        PublicPolicyRule.model_validate({**base, "rule_id": "P13"})
