"""Integration and System Precedence Tests for Policy Engine (Phase 2C.7: INT-01 to INT-10)."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.config import settings
from app.core.security import proposal_content_hash, sign_payload
from app.schemas.mandate import Mandate, MandateCreate, MandateStatus
from app.schemas.policy import (
    DecisionOutcome,
    EvaluationContext,
    OracleResult,
    RuleStatus,
)
from app.schemas.proposal import Proposal, ProposalCreate, ProposalStatus
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.policy_engine import (
    evaluate_policy,
    generate_approval_token,
    hash_approval_token,
    register_approval_token,
    revalidate_checkout_price,
    verify_approval_token,
    verify_and_consume_approval_token,
)
from app.services.proposal_service import (
    create_proposal,
    evaluate_proposal_service,
    get_proposal,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def base_now() -> datetime:
    return datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


def _make_mandate_payload(
    budget: Decimal = Decimal("50.00"),
    require_approval: bool = False,
    merchants: list[str] | None = None,
    categories: list[str] | None = None,
) -> MandateCreate:
    return MandateCreate(
        max_total_budget=budget,
        currency="USD",
        allowed_merchants=merchants or ["amazon.com"],
        allowed_categories=categories or ["books"],
        max_item_count=5,
        max_quantity_per_item=2,
        max_price_per_item=Decimal("25.00"),
        allow_subscriptions=False,
        require_user_approval=require_approval,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )


def _make_proposal_payload(mandate_id: str, **overrides: object) -> ProposalCreate:
    data = {
        "mandate_id": mandate_id,
        "product_id": "sku-100",
        "product_name": "Hardcover Notebook",
        "merchant": "amazon.com",
        "category": "books",
        "quantity": 1,
        "unit_price": Decimal("12.50"),
        "currency": "USD",
        "tax": Decimal("1.00"),
        "shipping": Decimal("2.00"),
        "other_charges": Decimal("0.50"),
        "product_source": "https://amazon.com/dp/sku-100",
        "idempotency_key": "api-int-req-1",
    }
    data.update(overrides)
    return ProposalCreate(**data)


async def _setup_active_mandate(
    require_approval: bool = False,
    budget: Decimal = Decimal("50.00"),
) -> Mandate:
    draft = await create_draft_mandate(
        _make_mandate_payload(budget=budget, require_approval=require_approval)
    )
    confirmed = await confirm_mandate(draft.mandate_id)
    signed = await sign_mandate(confirmed.mandate_id)
    return await activate_mandate(signed.mandate_id)


# --- INT-01: Clean evaluation with require_user_approval == False -> ALLOW ---
async def test_int_01_clean_evaluation_allows() -> None:
    mandate = await _setup_active_mandate(require_approval=False)
    proposal, _ = await create_proposal(_make_proposal_payload(mandate.mandate_id))

    decision = await evaluate_proposal_service(proposal.proposal_id)
    assert decision.decision == DecisionOutcome.ALLOW
    assert len(decision.rules) == 12
    assert decision.blocking_rules == []
    assert decision.approval_required_rules == []

    # Proposal state is advanced to APPROVED
    updated = await get_proposal(proposal.proposal_id)
    assert updated.status == ProposalStatus.APPROVED


# --- INT-02: Clean evaluation with require_user_approval == True -> REQUIRE_APPROVAL ---
async def test_int_02_clean_evaluation_requires_approval() -> None:
    mandate = await _setup_active_mandate(require_approval=True)
    proposal, _ = await create_proposal(_make_proposal_payload(mandate.mandate_id))

    decision = await evaluate_proposal_service(proposal.proposal_id)
    assert decision.decision == DecisionOutcome.REQUIRE_APPROVAL
    assert decision.blocking_rules == []
    assert decision.is_one_time_exception is False

    # Proposal state remains EVALUATED awaiting user sign-off
    updated = await get_proposal(proposal.proposal_id)
    assert updated.status == ProposalStatus.EVALUATED


# --- INT-03: Hard BLOCK overrides soft REQUIRE_APPROVAL ---
async def test_int_03_hard_block_overrides_soft() -> None:
    mandate = await _setup_active_mandate(require_approval=True)
    # Currency mismatch is a hard block
    proposal, _ = await create_proposal(_make_proposal_payload(mandate.mandate_id))

    # Artificially alter proposal currency to trigger P08 hard block
    # while P05 unlisted merchant is soft
    proposal_dict = proposal.model_dump()
    proposal_dict["currency"] = "EUR"
    proposal_dict["merchant"] = "target.com"  # soft exception
    proposal_dict["content_hash"] = proposal_content_hash(proposal_dict)
    tampered_proposal = Proposal(**proposal_dict)

    ctx = EvaluationContext(now=datetime.now(timezone.utc))
    decision = evaluate_policy(tampered_proposal, mandate, ctx)

    assert decision.decision == DecisionOutcome.BLOCK
    assert "P08" in decision.blocking_rules
    assert "P05" in decision.approval_required_rules


# --- INT-04: Multiple soft exceptions all require approval ---
async def test_int_04_multiple_soft_exceptions() -> None:
    mandate = await _setup_active_mandate(require_approval=True)
    # Target.com (unlisted merchant) + electronics (unlisted category)
    proposal, _ = await create_proposal(
        _make_proposal_payload(
            mandate.mandate_id,
            merchant="target.com",
            category="electronics",
        )
    )

    decision = await evaluate_proposal_service(proposal.proposal_id)
    assert decision.decision == DecisionOutcome.REQUIRE_APPROVAL
    assert "P05" in decision.approval_required_rules
    assert "P06" in decision.approval_required_rules
    assert decision.is_one_time_exception is True
    assert "MERCHANT_EXCEPTION" in decision.exception_types
    assert "CATEGORY_EXCEPTION" in decision.exception_types


# --- INT-05: Single-use approval token verification ---
async def test_int_05_approval_token_verification(base_now: datetime) -> None:
    proposal_id = "prp_test_token"
    content_hash = "hash_123456"
    session_id = "user_sess_789"
    nonce = "nonce_int_05_test"
    token = generate_approval_token(proposal_id, content_hash, session_id, base_now, nonce=nonce)

    valid, reason = verify_approval_token(
        token=token,
        proposal_id=proposal_id,
        content_hash=content_hash,
        user_session_id=session_id,
        approval_timestamp=base_now,
        nonce=nonce,
        now=base_now + timedelta(minutes=5),
    )
    assert valid is True
    assert "verified successfully" in reason


# --- INT-06: Approval token rejected after terms modified ---
async def test_int_06_approval_token_rejected_after_modification(base_now: datetime) -> None:
    proposal_id = "prp_test_token"
    content_hash = "hash_original"
    session_id = "user_sess_789"
    nonce = "nonce_int_06_test"
    token = generate_approval_token(proposal_id, content_hash, session_id, base_now, nonce=nonce)

    # Content hash changed because terms modified
    valid, reason = verify_approval_token(
        token=token,
        proposal_id=proposal_id,
        content_hash="hash_modified",
        user_session_id=session_id,
        approval_timestamp=base_now,
        nonce=nonce,
        now=base_now + timedelta(minutes=5),
    )
    assert valid is False
    assert "failed" in reason


# --- INT-07: Expired approval token (> 15m TTL) ---
async def test_int_07_expired_approval_token(base_now: datetime) -> None:
    proposal_id = "prp_test_token"
    content_hash = "hash_123"
    session_id = "user_sess_789"
    nonce = "nonce_int_07_test"
    token = generate_approval_token(proposal_id, content_hash, session_id, base_now, nonce=nonce)

    valid, reason = verify_approval_token(
        token=token,
        proposal_id=proposal_id,
        content_hash=content_hash,
        user_session_id=session_id,
        approval_timestamp=base_now,
        nonce=nonce,
        now=base_now + timedelta(minutes=16),  # Exceeds 15m TTL
    )
    assert valid is False
    assert "expired" in reason


# --- INT-08: Checkout revalidation fails on price drift ---
async def test_int_08_checkout_revalidation_drift_fails(base_now: datetime) -> None:
    proposal_data = {
        "proposal_id": "prp_drift",
        "mandate_id": "man_drift",
        "schema_version": "1.0",
        "status": ProposalStatus.EVALUATED,
        "product_id": "sku-100",
        "product_name": "Hardcover Notebook",
        "merchant": "amazon.com",
        "category": "books",
        "quantity": 1,
        "unit_price": Decimal("10.00"),
        "currency": "USD",
        "subtotal": Decimal("10.00"),
        "tax": Decimal("0.00"),
        "shipping": Decimal("0.00"),
        "other_charges": Decimal("0.00"),
        "total": Decimal("10.00"),
        "product_source": "https://amazon.com/dp/sku-100",
        "created_at": base_now,
        "fingerprint": "a" * 64,
        "content_hash": "",
    }
    proposal_data["content_hash"] = proposal_content_hash(proposal_data)
    proposal = Proposal(**proposal_data)

    # Oracle reports price jumped from $10 to $15 (50% increase)
    fresh_oracle = OracleResult(
        product_id="sku-100",
        live_price=Decimal("15.00"),
        fetched_at=base_now,
        is_available=True,
        is_stale=False,
    )
    valid, reason = revalidate_checkout_price(proposal, fresh_oracle, base_now)
    assert valid is False
    assert "exceeding tolerance" in reason


# --- INT-09: One-time merchant exception does NOT mutate mandate allowlist ---
async def test_int_09_merchant_exception_does_not_mutate_mandate() -> None:
    mandate = await _setup_active_mandate(require_approval=True)
    assert "target.com" not in mandate.allowed_merchants

    # Proposal A for target.com is evaluated
    proposal_a, _ = await create_proposal(
        _make_proposal_payload(
            mandate.mandate_id,
            merchant="target.com",
            idempotency_key="key-apple-1",
        )
    )
    decision_a = await evaluate_proposal_service(proposal_a.proposal_id)
    assert decision_a.decision == DecisionOutcome.REQUIRE_APPROVAL
    assert "MERCHANT_EXCEPTION" in decision_a.exception_types

    # Mandate in database must still NOT have target.com
    from app.services.mandate_service import get_mandate

    mandate_after = await get_mandate(mandate.mandate_id)
    assert "target.com" not in mandate_after.allowed_merchants

    # Proposal B for target.com still requires approval
    proposal_b, _ = await create_proposal(
        _make_proposal_payload(
            mandate.mandate_id,
            merchant="target.com",
            product_id="sku-200",
            idempotency_key="key-banana-2",
        )
    )
    decision_b = await evaluate_proposal_service(proposal_b.proposal_id)
    assert decision_b.decision == DecisionOutcome.REQUIRE_APPROVAL
    assert "MERCHANT_EXCEPTION" in decision_b.exception_types


# --- INT-10: Concurrency spend reservation atomicity ---
async def test_int_10_concurrent_spend_reservation() -> None:
    # Mandate budget is $40.00
    mandate = await _setup_active_mandate(require_approval=False, budget=Decimal("40.00"))

    # Proposal 1: total $25.00 (unit $20 + shipping $5)
    prop1, _ = await create_proposal(
        _make_proposal_payload(
            mandate.mandate_id,
            unit_price=Decimal("20.00"),
            shipping=Decimal("5.00"),
            other_charges=Decimal("0.00"),
            tax=Decimal("0.00"),
            idempotency_key="key-conc-1",
        )
    )
    # Proposal 2: total $25.00 (unit $20 + shipping $5)
    prop2, _ = await create_proposal(
        _make_proposal_payload(
            mandate.mandate_id,
            product_id="sku-2",
            unit_price=Decimal("20.00"),
            shipping=Decimal("5.00"),
            other_charges=Decimal("0.00"),
            tax=Decimal("0.00"),
            idempotency_key="key-conc-2",
        )
    )

    # Evaluate 1 -> succeeds ($25 <= $40)
    decision1 = await evaluate_proposal_service(prop1.proposal_id)
    assert decision1.decision == DecisionOutcome.ALLOW

    # Evaluate 2 -> cumulative spend is $25 + $25 = $50 > $40 -> BLOCKED by P03!
    decision2 = await evaluate_proposal_service(prop2.proposal_id)
    assert decision2.decision == DecisionOutcome.BLOCK
    assert "P03" in decision2.blocking_rules



# --- API Endpoint Test ---
async def test_api_evaluate_proposal_endpoint(client: httpx.AsyncClient) -> None:
    """POST /api/v1/proposals/{proposal_id}/evaluate returns PolicyDecision."""
    # Obtain authenticated session for mandate creation
    sess_res = await client.post("/api/v1/sessions", json={})
    auth_headers = {"Authorization": f"Bearer {sess_res.json()['session_token']}"}

    # Create mandate via API
    mandate_res = await client.post(
        "/api/v1/mandates",
        json={
            "max_total_budget": "50.00",
            "currency": "USD",
            "allowed_merchants": ["amazon.com"],
            "allowed_categories": ["books"],
            "max_item_count": 2,
            "max_quantity_per_item": 1,
            "require_user_approval": False,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        },
        headers=auth_headers,
    )
    mandate_id = mandate_res.json()["mandate_id"]
    await client.post(f"/api/v1/mandates/{mandate_id}/confirm", headers=auth_headers)
    await client.post(f"/api/v1/mandates/{mandate_id}/sign", headers=auth_headers)
    await client.post(f"/api/v1/mandates/{mandate_id}/activate", headers=auth_headers)

    # Create proposal via API
    prop_res = await client.post(
        "/api/v1/proposals",
        json={
            "mandate_id": mandate_id,
            "product_id": "sku-100",
            "product_name": "Hardcover Notebook",
            "merchant": "amazon.com",
            "category": "books",
            "quantity": 1,
            "unit_price": "12.50",
            "currency": "USD",
            "tax": "1.00",
            "shipping": "2.00",
            "other_charges": "0.50",
            "product_source": "https://amazon.com/dp/sku-100",
            "idempotency_key": "api-eval-test-1",
        },
        headers=auth_headers,
    )
    proposal_id = prop_res.json()["proposal_id"]

    # Evaluate via API
    eval_res = await client.post(
        f"/api/v1/proposals/{proposal_id}/evaluate", headers=auth_headers
    )
    assert eval_res.status_code == 200
    data = eval_res.json()
    assert data["decision"] == "ALLOW"
    assert len(data["rules"]) == 12
    assert data["proposal_id"] == proposal_id
    assert data["mandate_id"] == mandate_id


# --- Remediation Tests: Concurrency, Double-Counting, and Approval Tokens ---

async def test_remediation_p10_daily_limit_no_double_counting() -> None:
    """Verify evaluating a proposal does not double-count its own amount against daily limit."""
    mandate = await _setup_active_mandate(require_approval=False, budget=Decimal("100.00"))
    # Proposal total: unit_price 20 + shipping 5 + tax 5 = 30.00
    proposal, _ = await create_proposal(
        _make_proposal_payload(
            mandate.mandate_id,
            product_id="sku-no-double",
            unit_price=Decimal("20.00"),
            shipping=Decimal("5.00"),
            tax=Decimal("5.00"),
            other_charges=Decimal("0.00"),
            idempotency_key="key-no-double-1",
        )
    )
    # Daily limit is 35.00.
    # Previously, proposal total (30) was in today_reserved AND added again (30 + 30 = 60 > 35).
    # With exclude_proposal_id, today_reserved is 0.00, projected is 30.00 <= 35.00 -> ALLOW!
    decision = await evaluate_proposal_service(
        proposal.proposal_id, daily_limit=Decimal("35.00")
    )
    assert decision.decision == DecisionOutcome.ALLOW
    p10 = next(r for r in decision.rules if r.rule_id == "P10")
    assert p10.status == RuleStatus.PASS
    assert p10.evidence["today_reserved"] == "0.00"
    assert p10.evidence["projected_spend"] == "30.00"


async def test_remediation_concurrent_evaluations_atomic_reservation() -> None:
    """Verify concurrent evaluations cannot race to overspend the mandate budget."""
    # Mandate budget $40.00. Two proposals for $25.00 each.
    mandate = await _setup_active_mandate(require_approval=False, budget=Decimal("40.00"))
    prop1, _ = await create_proposal(
        _make_proposal_payload(
            mandate.mandate_id,
            product_id="sku-race-1",
            unit_price=Decimal("25.00"),
            shipping=Decimal("0.00"),
            tax=Decimal("0.00"),
            other_charges=Decimal("0.00"),
            idempotency_key="key-race-1",
        )
    )
    prop2, _ = await create_proposal(
        _make_proposal_payload(
            mandate.mandate_id,
            product_id="sku-race-2",
            unit_price=Decimal("25.00"),
            shipping=Decimal("0.00"),
            tax=Decimal("0.00"),
            other_charges=Decimal("0.00"),
            idempotency_key="key-race-2",
        )
    )

    # Concurrently evaluate both proposals
    decisions = await asyncio.gather(
        evaluate_proposal_service(prop1.proposal_id),
        evaluate_proposal_service(prop2.proposal_id),
    )

    outcomes = [d.decision for d in decisions]
    # Under atomic SQLite BEGIN IMMEDIATE locking, exactly one proposal must ALLOW,
    # and the other MUST BLOCK due to budget exhaustion (P03).
    assert DecisionOutcome.ALLOW in outcomes
    assert DecisionOutcome.BLOCK in outcomes

    # Verify database final statuses reflect this
    updated1 = await get_proposal(prop1.proposal_id)
    updated2 = await get_proposal(prop2.proposal_id)
    statuses = {updated1.status, updated2.status}
    assert statuses == {ProposalStatus.APPROVED, ProposalStatus.REJECTED}


async def test_remediation_persistent_approval_token_single_use() -> None:
    """Verify persistent approval token single-use tracking and replay prevention."""
    mandate = await _setup_active_mandate(require_approval=True)
    proposal, _ = await create_proposal(_make_proposal_payload(mandate.mandate_id))

    now = datetime.now(timezone.utc)
    nonce = "nonce_rem_single_use"
    token = generate_approval_token(
        proposal_id=proposal.proposal_id,
        content_hash=proposal.content_hash,
        user_session_id="user_sess_123",
        approval_timestamp=now,
        nonce=nonce,
    )

    # Pre-register token
    await register_approval_token(
        token=token,
        proposal_id=proposal.proposal_id,
        approval_timestamp=now,
        ttl_seconds=900,
    )

    # 1. First consumption succeeds
    success, reason = await verify_and_consume_approval_token(
        token=token,
        proposal_id=proposal.proposal_id,
        content_hash=proposal.content_hash,
        user_session_id="user_sess_123",
        approval_timestamp=now,
        nonce=nonce,
        now=now,
    )
    assert success is True
    assert "consumed successfully" in reason

    # 2. Replay attempt with same token fails
    success_replay, reason_replay = await verify_and_consume_approval_token(
        token=token,
        proposal_id=proposal.proposal_id,
        content_hash=proposal.content_hash,
        user_session_id="user_sess_123",
        approval_timestamp=now,
        nonce=nonce,
        now=now + timedelta(seconds=1),
    )
    assert success_replay is False
    assert "replay detected" in reason_replay


async def test_remediation_approval_token_concurrent_replay_prevention() -> None:
    """Verify simultaneous concurrent consumption of same token is safely serialized."""
    mandate = await _setup_active_mandate(require_approval=True)
    proposal, _ = await create_proposal(
        _make_proposal_payload(mandate.mandate_id, idempotency_key="key-tok-conc")
    )

    now = datetime.now(timezone.utc)
    nonce = "nonce_rem_concurrent"
    token = generate_approval_token(
        proposal_id=proposal.proposal_id,
        content_hash=proposal.content_hash,
        user_session_id="sess_conc_456",
        approval_timestamp=now,
        nonce=nonce,
    )

    # Two concurrent requests presenting the same approval token
    results = await asyncio.gather(
        verify_and_consume_approval_token(
            token=token,
            proposal_id=proposal.proposal_id,
            content_hash=proposal.content_hash,
            user_session_id="sess_conc_456",
            approval_timestamp=now,
            nonce=nonce,
            now=now,
        ),
        verify_and_consume_approval_token(
            token=token,
            proposal_id=proposal.proposal_id,
            content_hash=proposal.content_hash,
            user_session_id="sess_conc_456",
            approval_timestamp=now,
            nonce=nonce,
            now=now,
        ),
    )

    # Exactly one must succeed, the other must fail with replay
    success_flags = [r[0] for r in results]
    assert success_flags.count(True) == 1
    assert success_flags.count(False) == 1
