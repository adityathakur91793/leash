"""Unit tests for Policy Engine Rule P09: Live Price Validation & Revalidation (Phase 2C.3)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.schemas.mandate import Mandate, MandateStatus
from app.schemas.policy import EvaluationContext, OracleResult, RuleStatus
from app.schemas.proposal import Proposal, ProposalStatus
from app.services.policy_engine import evaluate_p09_live_price, revalidate_checkout_price


@pytest.fixture
def base_now() -> datetime:
    return datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def base_mandate(base_now: datetime) -> Mandate:
    return Mandate(
        mandate_id="man_test_p09",
        schema_version="1.0",
        status=MandateStatus.ACTIVE,
        max_total_budget=Decimal("100.00"),
        currency="USD",
        allowed_merchants=["amazon.com"],
        allowed_categories=["books"],
        max_item_count=5,
        max_quantity_per_item=2,
        allow_subscriptions=False,
        require_user_approval=False,
        created_at=base_now - timedelta(days=1),
        expires_at=base_now + timedelta(days=1),
    )


@pytest.fixture
def base_proposal(base_now: datetime) -> Proposal:
    return Proposal(
        proposal_id="prp_test_p09",
        mandate_id="man_test_p09",
        schema_version="1.0",
        status=ProposalStatus.SUBMITTED,
        product_id="sku-100",
        product_name="Hardcover Notebook",
        merchant="amazon.com",
        category="books",
        quantity=1,
        unit_price=Decimal("12.50"),
        currency="USD",
        subtotal=Decimal("12.50"),
        tax=Decimal("1.00"),
        shipping=Decimal("2.00"),
        other_charges=Decimal("0.50"),
        total=Decimal("16.00"),
        product_source="https://amazon.com/dp/sku-100",
        created_at=base_now,
        fingerprint="a" * 64,
        content_hash="b" * 64,
    )


def test_p09_01_within_5_percent_tolerance(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """P09-01: Price difference 0.4% (12.50 vs 12.45) is PASS."""
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("12.45"),
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    ctx = EvaluationContext(now=base_now, oracle_result=oracle)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.PASS
    assert "within tolerance" in res.reason


def test_p09_02_exact_boundary(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """P09-02: Price difference near 5% boundary is PASS."""
    # 12.50 vs 11.905 -> (12.50 - 11.905) / 11.905 = 0.04997 <= 0.05
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("11.905"),
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    ctx = EvaluationContext(now=base_now, oracle_result=oracle)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.PASS


def test_p09_03_exceeds_tolerance_no_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """P09-03: 20% variance (12.50 vs 10.00) without approval is BLOCK."""
    base_mandate.require_user_approval = False
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("10.00"),
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    ctx = EvaluationContext(now=base_now, oracle_result=oracle)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.BLOCK
    assert "exceeds" in res.reason


def test_p09_04_exceeds_tolerance_with_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """P09-04: 20% variance with approval enabled is REQUIRE_APPROVAL."""
    base_mandate.require_user_approval = True
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("10.00"),
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    ctx = EvaluationContext(now=base_now, oracle_result=oracle)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.REQUIRE_APPROVAL
    assert "requires user approval" in res.reason


def test_p09_05_oracle_unavailable(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """P09-05: Unreachable oracle produces NOT_APPLICABLE (never false PASS)."""
    oracle = OracleResult(
        product_id="sku-100",
        live_price=None,
        fetched_at=base_now,
        is_available=False,
        is_stale=False,
    )
    ctx = EvaluationContext(now=base_now, oracle_result=oracle)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.NOT_APPLICABLE
    assert "unavailable" in res.reason


def test_p09_06_oracle_stale(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """P09-06: Stale oracle reading produces NOT_APPLICABLE."""
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("12.50"),
        fetched_at=base_now - timedelta(minutes=6),
        is_available=True,
        is_stale=True,
    )
    ctx = EvaluationContext(now=base_now, oracle_result=oracle)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.NOT_APPLICABLE
    assert "stale" in res.reason


def test_p09_07_product_not_indexed(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """P09-07: Unindexed product produces NOT_APPLICABLE."""
    oracle = OracleResult(
        product_id="sku-100",
        live_price=None,
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    ctx = EvaluationContext(now=base_now, oracle_result=oracle)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.NOT_APPLICABLE
    assert "not indexed" in res.reason


def test_p09_08_oracle_invalid_price(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """P09-08: Non-positive oracle price produces BLOCK."""
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("0.00"),
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    ctx = EvaluationContext(now=base_now, oracle_result=oracle)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.BLOCK
    assert "invalid non-positive" in res.reason


def test_p09_no_oracle_configured(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    """When context has no oracle_result, returns NOT_APPLICABLE."""
    ctx = EvaluationContext(now=base_now, oracle_result=None)
    res = evaluate_p09_live_price(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.NOT_APPLICABLE


def test_revalidate_checkout_price_success(base_proposal: Proposal, base_now: datetime) -> None:
    """Checkout revalidation passes when live price is within tolerance."""
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("12.48"),
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    valid, reason = revalidate_checkout_price(base_proposal, oracle, base_now)
    assert valid is True
    assert "revalidated successfully" in reason


def test_revalidate_checkout_price_drift_exceeded(
    base_proposal: Proposal, base_now: datetime
) -> None:
    """Checkout revalidation fails when price drifted > 5%."""
    oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("10.00"),  # 20% drift
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    valid, reason = revalidate_checkout_price(base_proposal, oracle, base_now)
    assert valid is False
    assert "Price drifted" in reason
