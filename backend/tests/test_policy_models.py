"""Unit tests for Policy Engine schemas, models, and decision aggregation (Phase 2C.1)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.policy import (
    DecisionOutcome,
    EvaluationContext,
    OracleResult,
    PolicyDecision,
    RuleResult,
    RuleStatus,
    aggregate_decision,
)


def _make_rule_result(
    rule_id: str,
    status: RuleStatus,
    reason: str = "Test reason",
    evidence: dict | None = None,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_name=f"Rule {rule_id}",
        status=status,
        reason=reason,
        evidence=evidence or {},
        evaluated_at=datetime.now(timezone.utc),
    )


def test_rule_result_requires_utc_timestamp() -> None:
    """Naive datetimes without timezone must be rejected."""
    naive_dt = datetime(2026, 11, 4, 12, 0, 0)
    with pytest.raises(ValidationError):
        RuleResult(
            rule_id="P01",
            rule_name="Signature",
            status=RuleStatus.PASS,
            reason="OK",
            evaluated_at=naive_dt,
        )


def test_rule_result_extra_fields_forbidden() -> None:
    """Extra fields must be rejected (ConfigDict extra='forbid')."""
    with pytest.raises(ValidationError):
        RuleResult(
            rule_id="P01",
            rule_name="Signature",
            status=RuleStatus.PASS,
            reason="OK",
            evaluated_at=datetime.now(timezone.utc),
            unauthorized_field="malicious",  # type: ignore[call-arg]
        )


def test_oracle_result_valid() -> None:
    """Valid oracle result preserves Decimal price and UTC timestamp."""
    now = datetime.now(timezone.utc)
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("12.50"),
        fetched_at=now,
        is_available=True,
        is_stale=False,
    )
    assert oracle.product_id == "sku-100"
    assert oracle.live_price == Decimal("12.50")
    assert oracle.fetched_at == now


def test_evaluation_context_defaults() -> None:
    """Context has sensible defaults and validates UTC."""
    now = datetime.now(timezone.utc)
    ctx = EvaluationContext(now=now)
    assert ctx.today_reserved == Decimal("0.00")
    assert ctx.daily_txn_limit is None
    assert ctx.cumulative_mandate_spent == Decimal("0.00")
    assert ctx.oracle_result is None


def test_aggregate_decision_all_pass_no_approval_needed() -> None:
    """When all rules pass and mandate does not require approval -> ALLOW."""
    now = datetime.now(timezone.utc)
    rules = [
        _make_rule_result("P01", RuleStatus.PASS),
        _make_rule_result("P02", RuleStatus.PASS),
        _make_rule_result("P03", RuleStatus.PASS),
        _make_rule_result("P04", RuleStatus.NOT_APPLICABLE),
    ]
    decision = aggregate_decision(
        proposal_id="prp_1",
        mandate_id="man_1",
        rules=rules,
        mandate_require_user_approval=False,
        decision_at=now,
    )
    assert decision.decision == DecisionOutcome.ALLOW
    assert decision.blocking_rules == []
    assert decision.approval_required_rules == []
    assert decision.is_one_time_exception is False


def test_aggregate_decision_all_pass_mandate_requires_approval() -> None:
    """When all rules pass but mandate requires approval -> REQUIRE_APPROVAL."""
    now = datetime.now(timezone.utc)
    rules = [
        _make_rule_result("P01", RuleStatus.PASS),
        _make_rule_result("P02", RuleStatus.PASS),
    ]
    decision = aggregate_decision(
        proposal_id="prp_1",
        mandate_id="man_1",
        rules=rules,
        mandate_require_user_approval=True,
        decision_at=now,
    )
    assert decision.decision == DecisionOutcome.REQUIRE_APPROVAL
    assert decision.blocking_rules == []
    assert decision.is_one_time_exception is False


def test_aggregate_decision_hard_block_overrides_everything() -> None:
    """BLOCK overrides both PASS and REQUIRE_APPROVAL."""
    now = datetime.now(timezone.utc)
    rules = [
        _make_rule_result("P01", RuleStatus.BLOCK, reason="Signature forged"),
        _make_rule_result("P04", RuleStatus.REQUIRE_APPROVAL),
        _make_rule_result("P05", RuleStatus.PASS),
    ]
    decision = aggregate_decision(
        proposal_id="prp_1",
        mandate_id="man_1",
        rules=rules,
        mandate_require_user_approval=True,
        decision_at=now,
    )
    assert decision.decision == DecisionOutcome.BLOCK
    assert "P01" in decision.blocking_rules
    assert "P04" in decision.approval_required_rules


def test_aggregate_decision_one_time_exceptions_tracked() -> None:
    """Soft exceptions generate REQUIRE_APPROVAL and track exception types."""
    now = datetime.now(timezone.utc)
    rules = [
        _make_rule_result("P01", RuleStatus.PASS),
        _make_rule_result("P05", RuleStatus.REQUIRE_APPROVAL, reason="Unlisted merchant"),
        _make_rule_result("P06", RuleStatus.REQUIRE_APPROVAL, reason="Unlisted category"),
    ]
    decision = aggregate_decision(
        proposal_id="prp_1",
        mandate_id="man_1",
        rules=rules,
        mandate_require_user_approval=False,
        decision_at=now,
    )
    assert decision.decision == DecisionOutcome.REQUIRE_APPROVAL
    assert decision.is_one_time_exception is True
    assert "MERCHANT_EXCEPTION" in decision.exception_types
    assert "CATEGORY_EXCEPTION" in decision.exception_types
