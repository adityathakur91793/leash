"""Tests for One-Time Exception Approval API and service."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.config import settings
from app.db.database import get_proposal_from_db, list_audit_log_entries
from app.schemas.audit import AuditEventType
from app.schemas.mandate import MandateCreate
from app.schemas.proposal import ProposalCreate, ProposalStatus
from app.services.kill_switch_service import disengage_kill_switch, engage_kill_switch
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.proposal_service import create_proposal


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
async def test_consume_approval_token_success_and_replay_prevention(
    client: httpx.AsyncClient,
) -> None:
    """Valid token is consumed, transitions proposal to APPROVED, and rejects replay."""
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

    # Verify proposal status transitioned to APPROVED in database
    updated_prop = await get_proposal_from_db(proposal_id)
    assert updated_prop is not None
    assert updated_prop.status == ProposalStatus.APPROVED

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
    """Token issued to one principal cannot be consumed by an impostor principal (403 Forbidden)."""
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

    consume_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approve",
        json={
            "proposal_id": proposal_id,
            "token": token_data["token"],
            "issued_at": token_data["issued_at"],
            "nonce": token_data["nonce"],
        },
        headers=impostor_headers,
    )
    assert consume_resp.status_code == 403
    assert "Forbidden" in consume_resp.json()["detail"]

    issue_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approval-token",
        json={"proposal_id": proposal_id},
        headers=legit_headers,
    )
    assert issue_resp.status_code == 200
    token_data = issue_resp.json()

    consume_resp = await client.post(
        f"/api/v1/proposals/{proposal_id}/approve",
        json={
            "proposal_id": proposal_id,
            "token": token_data["token"],
            "issued_at": token_data["issued_at"],
            "nonce": token_data["nonce"],
        },
        headers=impostor_headers,
    )
    assert consume_resp.status_code == 403
    assert "Forbidden" in consume_resp.json()["detail"]
