"""Tests for One-Time Exception Approval API and service."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.config import settings
from app.db.database import get_connection, get_proposal_from_db, list_audit_log_entries
from app.schemas.audit import AuditEventType
from app.schemas.mandate import MandateCreate
from app.schemas.policy import DecisionOutcome
from app.schemas.proposal import ProposalCreate, ProposalStatus
from app.services.intent_auditor_client import IntentAuditorUnavailable
from app.services.kill_switch_service import disengage_kill_switch, engage_kill_switch
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.approval_service import consume_approval_token, issue_approval_token
from app.services.proposal_service import (
    ProposalTamperedError,
    create_proposal,
    evaluate_proposal_service,
)


async def _auth_header(client: httpx.AsyncClient) -> tuple[dict[str, str], str]:
    """Helper to create session and return Bearer auth header and principal ID."""
    res = await client.post("/api/v1/sessions", json={})
    token = res.json()["session_token"]
    user_id = res.json()["user_id"]
    return {"Authorization": f"Bearer {token}"}, user_id


async def _setup_soft_approval_proposal(
    owner_user_id: str,
) -> tuple[str, str]:
    """Helper: create mandate with require_user_approval=True and proposal exceeding limit."""
    mandate_data = MandateCreate(
        max_total_budget=Decimal("500.00"),
        currency="USD",
        allowed_merchants=["bestbuy.com"],
        allowed_categories=["electronics"],
        max_item_count=5,
        max_quantity_per_item=2,
        max_price_per_item=Decimal("50.00"),
        allow_subscriptions=False,
        require_user_approval=True,  # Triggers REQUIRE_APPROVAL for over-limit item
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    mandate = await create_draft_mandate(mandate_data, owner_user_id=owner_user_id)
    await confirm_mandate(mandate.mandate_id)
    await sign_mandate(mandate.mandate_id)
    await activate_mandate(mandate.mandate_id)

    # Unit price $75 > max_price_per_item $50 -> REQUIRE_APPROVAL (Soft Check P04)
    proposal_data = ProposalCreate(
        mandate_id=mandate.mandate_id,
        product_id="sku-headphones-1",
        product_name="Noise Cancelling Headphones",
        merchant="bestbuy.com",
        category="electronics",
        quantity=1,
        unit_price=Decimal("75.00"),
        currency="USD",
        shipping=Decimal("0.00"),
        tax=Decimal("0.00"),
        other_charges=Decimal("0.00"),
        product_source="https://bestbuy.com/headphones",
    )
    proposal, _ = await create_proposal(proposal_data)
    await evaluate_proposal_service(proposal.proposal_id)
    return proposal.proposal_id, mandate.mandate_id


async def _setup_hard_block_proposal(
    owner_user_id: str,
) -> str:
    """Helper: create proposal that triggers a hard BLOCK (e.g. currency mismatch P08)."""
    mandate_data = MandateCreate(
        max_total_budget=Decimal("100.00"),
        currency="USD",
        allowed_merchants=["target.com"],
        allowed_categories=["groceries"],
        max_item_count=5,
        max_quantity_per_item=2,
        max_price_per_item=Decimal("50.00"),
        allow_subscriptions=False,
        require_user_approval=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    mandate = await create_draft_mandate(mandate_data, owner_user_id=owner_user_id)
    await confirm_mandate(mandate.mandate_id)
    await sign_mandate(mandate.mandate_id)
    await activate_mandate(mandate.mandate_id)

    # Total $150 > max_total_budget $100 -> Hard BLOCK rule P03
    proposal_data = ProposalCreate(
        mandate_id=mandate.mandate_id,
        product_id="sku-chair-1",
        product_name="Office Chair",
        merchant="target.com",
        category="groceries",
        quantity=1,
        unit_price=Decimal("150.00"),
        currency="USD",
        shipping=Decimal("0.00"),
        tax=Decimal("0.00"),
        other_charges=Decimal("0.00"),
        product_source="https://target.com/chair",
    )
    proposal, _ = await create_proposal(proposal_data)
    return proposal.proposal_id


async def _setup_allow_proposal(owner_user_id: str) -> str:
    """Create an evaluated proposal whose current deterministic decision is ALLOW."""
    mandate = await create_draft_mandate(
        MandateCreate(
            max_total_budget=Decimal("500.00"),
            currency="USD",
            allowed_merchants=["bestbuy.com"], allowed_categories=["electronics"],
            max_item_count=5, max_quantity_per_item=2,
            max_price_per_item=Decimal("100.00"), allow_subscriptions=False,
            require_user_approval=False,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        ),
        owner_user_id=owner_user_id,
    )
    await confirm_mandate(mandate.mandate_id)
    await sign_mandate(mandate.mandate_id)
    await activate_mandate(mandate.mandate_id)
    proposal, _ = await create_proposal(
        ProposalCreate(
            mandate_id=mandate.mandate_id,
            product_id="sku-allow-1",
            product_name="Compliant headphones",
            merchant="bestbuy.com",
            category="electronics",
            quantity=1,
            unit_price=Decimal("25.00"),
            currency="USD",
            product_source="https://bestbuy.com/headphones",
        )
    )
    decision = await evaluate_proposal_service(proposal.proposal_id)
    assert decision.decision == DecisionOutcome.ALLOW
    return proposal.proposal_id


@pytest.mark.asyncio
async def test_issue_approval_token_success(client: httpx.AsyncClient) -> None:
    """Issue a valid one-time approval token for a soft-approval proposal."""
    headers, principal_id = await _auth_header(client)
    proposal_id, _ = await _setup_soft_approval_proposal(owner_user_id=principal_id)

    response = await client.post(
        f"/api/v1/proposals/{proposal_id}/approval-token",
        json={"proposal_id": proposal_id},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["proposal_id"] == proposal_id
    assert "token" in data
    assert len(data["token"]) == 64  # SHA-256 hex string
    assert "nonce" in data
    assert len(data["nonce"]) == 32  # 16-byte hex nonce
    assert "issued_at" in data
    assert "expires_at" in data


@pytest.mark.asyncio
async def test_issue_approval_token_rejects_hard_block(client: httpx.AsyncClient) -> None:
    """Hard BLOCK rules (e.g. P03 budget ceiling) cannot be issued approval tokens."""
    headers, principal_id = await _auth_header(client)
    hard_block_pid = await _setup_hard_block_proposal(owner_user_id=principal_id)

    response = await client.post(
        f"/api/v1/proposals/{hard_block_pid}/approval-token",
        json={"proposal_id": hard_block_pid},
        headers=headers,
    )
    assert response.status_code == 400
    assert "Hard BLOCK rule P03" in response.json()["detail"]


@pytest.mark.asyncio
async def test_issue_approval_token_rejects_allow(client: httpx.AsyncClient) -> None:
    headers, principal_id = await _auth_header(client)
    proposal_id = await _setup_allow_proposal(principal_id)
    response = await client.post(
        f"/api/v1/proposals/{proposal_id}/approval-token",
        json={"proposal_id": proposal_id}, headers=headers,
    )
    assert response.status_code == 400
    assert "ALLOW" in response.json()["detail"]


@pytest.mark.parametrize("changed_outcome", [DecisionOutcome.ALLOW, DecisionOutcome.BLOCK])
@pytest.mark.asyncio
async def test_policy_change_invalidates_issued_token(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
    changed_outcome: DecisionOutcome,
) -> None:
    """Consumption re-evaluates eligibility and never consumes an old token."""
    import app.services.approval_service as approval_service

    headers, principal_id = await _auth_header(client)
    proposal_id, _ = await _setup_soft_approval_proposal(principal_id)
    issued = await issue_approval_token(proposal_id, principal_id)
    real_decision = approval_service.current_policy_decision

    async def changed_policy(*args, **kwargs):
        decision = await real_decision(*args, **kwargs)
        return decision.model_copy(update={"decision": changed_outcome})

    monkeypatch.setattr(approval_service, "current_policy_decision", changed_policy)
    success, reason = await consume_approval_token(
        proposal_id, issued["token"], principal_id, issued["issued_at"], issued["nonce"]
    )
    assert success is False
    assert changed_outcome.value in reason
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT consumed_at FROM approval_tokens WHERE proposal_id = ?",
            (proposal_id,),
        )).fetchone()
    assert row[0] is None
    assert (await client.get(
        f"/api/v1/proposals/{proposal_id}", headers=headers
    )).json()["status"] == "EVALUATED"


@pytest.mark.asyncio
async def test_consume_approval_token_success_and_replay_prevention(
    client: httpx.AsyncClient,
) -> None:
    """Valid token records exception review without recording purchase consent."""
    headers, principal_id = await _auth_header(client)
    proposal_id, _ = await _setup_soft_approval_proposal(owner_user_id=principal_id)

    # 1. Issue token
    issue_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approval-token",
        json={"proposal_id": proposal_id},
        headers=headers,
    )
    assert issue_resp.status_code == 200
    token_data = issue_resp.json()

    # 2. First consumption: must succeed
    consume_payload = {
        "proposal_id": proposal_id,
        "token": token_data["token"],
        "issued_at": token_data["issued_at"],
        "nonce": token_data["nonce"],
    }
    consume_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approve",
        json=consume_payload,
        headers=headers,
    )
    assert consume_resp.status_code == 200
    assert consume_resp.json()["success"] is True

    # Exception review is not purchase consent: proposal remains EVALUATED.
    updated_prop = await get_proposal_from_db(proposal_id)
    assert updated_prop is not None
    assert updated_prop.status == ProposalStatus.EVALUATED

    # 3. Second consumption (replay): must be rejected with 400
    replay_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approve",
        json=consume_payload,
        headers=headers,
    )
    assert replay_resp.status_code == 400
    assert "replay detected" in replay_resp.json()["detail"]

    # Verify REPLAY_ATTEMPT_BLOCKED audit entry exists
    replays, count = await list_audit_log_entries(
        event_type=AuditEventType.REPLAY_ATTEMPT_BLOCKED
    )
    assert count >= 1


@pytest.mark.asyncio
async def test_consume_approval_token_blocked_by_kill_switch(client: httpx.AsyncClient) -> None:
    """When kill switch is active, approval token consumption fails closed with 503."""
    headers, principal_id = await _auth_header(client)
    proposal_id, _ = await _setup_soft_approval_proposal(owner_user_id=principal_id)

    issue_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approval-token",
        json={"proposal_id": proposal_id},
        headers=headers,
    )
    assert issue_resp.status_code == 200
    token_data = issue_resp.json()

    # Engage kill switch
    await engage_kill_switch("sec_admin", "Emergency lockdown active")

    try:
        consume_resp = await client.post(
            f"/api/v1/proposals/{proposal_id}/approve",
            json={
                "proposal_id": proposal_id,
                "token": token_data["token"],
                "issued_at": token_data["issued_at"],
                "nonce": token_data["nonce"],
            },
            headers=headers,
        )
        assert consume_resp.status_code == 503
        assert "Emergency kill switch is engaged" in consume_resp.json()["detail"]
    finally:
        # Disengage for subsequent tests
        await disengage_kill_switch("sec_admin", "Clearing lockdown")


@pytest.mark.asyncio
async def test_consume_approval_token_rejects_altered_nonce(client: httpx.AsyncClient) -> None:
    """Tampering with the nonce invalidates the HMAC signature."""
    headers, principal_id = await _auth_header(client)
    proposal_id, _ = await _setup_soft_approval_proposal(owner_user_id=principal_id)

    issue_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approval-token",
        json={"proposal_id": proposal_id},
        headers=headers,
    )
    token_data = issue_resp.json()

    consume_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approve",
        json={
            "proposal_id": proposal_id,
            "token": token_data["token"],
            "issued_at": token_data["issued_at"],
            "nonce": "tampered_nonce_value_1234567890",
        },
        headers=headers,
    )
    assert consume_resp.status_code == 400
    assert "verification failed" in consume_resp.json()["detail"]


@pytest.mark.asyncio
async def test_consume_approval_token_rejects_wrong_session(client: httpx.AsyncClient) -> None:
    """Foreign and missing proposals are indistinguishable on both approval routes."""
    legit_headers, legit_id = await _auth_header(client)
    impostor_headers, impostor_id = await _auth_header(client)
    assert legit_id != impostor_id

    proposal_id, _ = await _setup_soft_approval_proposal(owner_user_id=legit_id)

    issue_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approval-token",
        json={"proposal_id": proposal_id},
        headers=legit_headers,
    )
    assert issue_resp.status_code == 200
    token_data = issue_resp.json()

    missing_id = "proposal-does-not-exist"
    for endpoint, foreign_body, missing_body in (
        ("approval-token", {"proposal_id": proposal_id}, {"proposal_id": missing_id}),
        ("approve", {
            "proposal_id": proposal_id, "token": token_data["token"],
            "issued_at": token_data["issued_at"], "nonce": token_data["nonce"],
        }, {
            "proposal_id": missing_id, "token": token_data["token"],
            "issued_at": token_data["issued_at"], "nonce": token_data["nonce"],
        }),
    ):
        foreign = await client.post(
            f"/api/v1/proposals/{proposal_id}/{endpoint}",
            json=foreign_body, headers=impostor_headers,
        )
        missing = await client.post(
            f"/api/v1/proposals/{missing_id}/{endpoint}",
            json=missing_body, headers=impostor_headers,
        )
        assert foreign.status_code == missing.status_code == 404
        assert (
            foreign.json()["detail"]
            == missing.json()["detail"]
            == "Proposal not found."
        )


@pytest.mark.asyncio
async def test_one_users_token_cannot_approve_another_users_proposal(
    client: httpx.AsyncClient,
) -> None:
    headers_a, owner_a = await _auth_header(client)
    headers_b, owner_b = await _auth_header(client)
    proposal_a, _ = await _setup_soft_approval_proposal(owner_a)
    proposal_b, _ = await _setup_soft_approval_proposal(owner_b)
    issued = await client.post(
        f"/api/v1/proposals/{proposal_a}/approval-token",
        json={"proposal_id": proposal_a}, headers=headers_a,
    )
    assert issued.status_code == 200
    token_data = issued.json()

    response = await client.post(
        f"/api/v1/proposals/{proposal_b}/approve",
        json={
            "proposal_id": proposal_b, "token": token_data["token"],
            "issued_at": token_data["issued_at"], "nonce": token_data["nonce"],
        }, headers=headers_b,
    )
    assert response.status_code == 400
    assert "verification failed" in response.json()["detail"]
    async with get_connection() as db:
        rows = await (await db.execute(
            "SELECT proposal_id, consumed_at FROM approval_tokens "
            "WHERE proposal_id IN (?, ?) ORDER BY proposal_id",
            (proposal_a, proposal_b),
        )).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == proposal_a and rows[0][1] is None
    assert (await client.get(
        f"/api/v1/proposals/{proposal_b}", headers=headers_b
    )).json()["status"] == "EVALUATED"


@pytest.mark.parametrize("source", ["owner_lookup", "approval_service"])
@pytest.mark.asyncio
async def test_consume_maps_proposal_tampering_to_422(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    import app.api.v1.approval as approval_api

    headers, _ = await _auth_header(client)
    proposal_id = "controlled-proposal"

    async def tampered(*args, **kwargs):
        raise ProposalTamperedError("Stored proposal integrity verification failed.")

    async def owned(*args, **kwargs):
        return None

    if source == "owner_lookup":
        monkeypatch.setattr(approval_api, "get_proposal_for_owner", tampered)
    else:
        monkeypatch.setattr(approval_api, "get_proposal_for_owner", owned)
        monkeypatch.setattr(approval_api, "consume_approval_token", tampered)

    response = await client.post(
        f"/api/v1/proposals/{proposal_id}/approve",
        json={
            "proposal_id": proposal_id,
            "token": "opaque-test-token",
            "issued_at": "2026-11-08T00:00:00+00:00",
            "nonce": "opaque-test-nonce",
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Stored proposal integrity verification failed."


@pytest.mark.asyncio
async def test_consume_maps_auditor_unavailable_to_safe_503(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.v1.approval as approval_api

    headers, _ = await _auth_header(client)

    async def owned(*args, **kwargs):
        return None

    async def unavailable(*args, **kwargs):
        raise IntentAuditorUnavailable("provider diagnostic must not be returned")

    monkeypatch.setattr(approval_api, "get_proposal_for_owner", owned)
    monkeypatch.setattr(approval_api, "consume_approval_token", unavailable)
    response = await client.post(
        "/api/v1/proposals/controlled-proposal/approve",
        json={
            "proposal_id": "controlled-proposal",
            "token": "opaque-test-token",
            "issued_at": "2026-11-08T00:00:00+00:00",
            "nonce": "opaque-test-nonce",
        },
        headers=headers,
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "A current valid intent assessment is required."
