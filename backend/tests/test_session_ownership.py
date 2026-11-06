"""Tests for Mandate Ownership and Cross-Principal Security Invariants (Migration 6)."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiosqlite
import httpx
import pytest

from app.db.database import get_db_path, get_mandate_from_db
from app.schemas.mandate import MandateCreate
from app.schemas.proposal import ProposalCreate
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.proposal_service import create_proposal

pytestmark = pytest.mark.asyncio


async def _create_user_session(
    client: httpx.AsyncClient, auth_header: dict[str, str] | None = None
) -> tuple[dict[str, str], str]:
    """Helper to establish a session (new anonymous principal, or multi-session for existing)."""
    headers = auth_header or {}
    res = await client.post("/api/v1/sessions", json={}, headers=headers)
    token = res.json()["session_token"]
    user_id = res.json()["user_id"]
    return {"Authorization": f"Bearer {token}"}, user_id


async def _setup_soft_approval(owner_principal: str) -> tuple[str, str]:
    """Create mandate owned by owner_principal and a proposal requiring approval."""
    mandate = await create_draft_mandate(
        MandateCreate(
            max_total_budget=Decimal("500.00"),
            currency="USD",
            allowed_merchants=["store.com"],
            allowed_categories=["electronics"],
            max_item_count=5,
            max_quantity_per_item=2,
            max_price_per_item=Decimal("50.00"),
            allow_subscriptions=False,
            require_user_approval=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        ),
        owner_user_id=owner_principal,
    )
    await confirm_mandate(mandate.mandate_id)
    await sign_mandate(mandate.mandate_id)
    await activate_mandate(mandate.mandate_id)

    prop, _ = await create_proposal(
        ProposalCreate(
            mandate_id=mandate.mandate_id,
            product_id="sku-gadget-1",
            product_name="Gadget",
            merchant="store.com",
            category="electronics",
            quantity=1,
            unit_price=Decimal("75.00"),
            currency="USD",
            shipping=Decimal("0.00"),
            tax=Decimal("0.00"),
            other_charges=Decimal("0.00"),
            product_source="https://store.com/gadget",
        )
    )
    return prop.proposal_id, mandate.mandate_id


async def test_mandate_ownership_assignment_from_session(client: httpx.AsyncClient) -> None:
    """Mandate owner_user_id is derived exclusively from server-side session."""
    headers, principal_id = await _create_user_session(client)
    res = await client.post(
        "/api/v1/mandates",
        json={
            "max_total_budget": "100.00",
            "currency": "USD",
            "allowed_merchants": ["store.com"],
            "allowed_categories": ["electronics"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
        headers=headers,
    )
    assert res.status_code == 201
    mandate_data = res.json()
    assert mandate_data["owner_user_id"] == principal_id
    assert principal_id.startswith("usr_")


async def test_mandate_ownership_immutability(client: httpx.AsyncClient) -> None:
    """Ownership is immutable; subsequent status updates or upserts cannot reassign it."""
    mandate = await create_draft_mandate(
        MandateCreate(
            max_total_budget=Decimal("100.00"),
            currency="USD",
            allowed_merchants=["store.com"],
            allowed_categories=["electronics"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        ),
        owner_user_id="original_owner",
    )
    assert mandate.owner_user_id == "original_owner"

    # Confirm and Sign the mandate (exercises save_mandate_to_db upsert)
    confirmed = await confirm_mandate(mandate.mandate_id)
    assert confirmed.owner_user_id == "original_owner"

    signed = await sign_mandate(mandate.mandate_id)
    assert signed.owner_user_id == "original_owner"

    # Verify directly from database
    db_mandate = await get_mandate_from_db(mandate.mandate_id)
    assert db_mandate is not None
    assert db_mandate.owner_user_id == "original_owner"


async def test_multiple_sessions_same_principal(client: httpx.AsyncClient) -> None:
    """Multiple sessions for the same principal can both manage the principal's mandates."""
    # Session 1 bootstrap
    headers_1, principal_id = await _create_user_session(client)

    # Setup soft approval proposal owned by this principal
    prop_id, _ = await _setup_soft_approval(owner_principal=principal_id)

    # Session 1 issues approval token
    issue_res = await client.post(
        f"/api/v1/proposals/{prop_id}/approval-token",
        json={"proposal_id": prop_id},
        headers=headers_1,
    )
    assert issue_res.status_code == 200
    token_meta = issue_res.json()

    # Session 2 minted for the same principal via authenticated session creation
    headers_2, principal_id_2 = await _create_user_session(client, auth_header=headers_1)
    assert principal_id_2 == principal_id

    consume_res = await client.post(
        f"/api/v1/proposals/{prop_id}/approve",
        json={
            "proposal_id": prop_id,
            "token": token_meta["token"],
            "issued_at": token_meta["issued_at"],
            "nonce": token_meta["nonce"],
        },
        headers=headers_2,
    )
    assert consume_res.status_code == 200
    assert consume_res.json()["success"] is True


async def test_cross_principal_issue_token_rejected(client: httpx.AsyncClient) -> None:
    """Principal B cannot issue an approval token for Principal A's mandate (403 Forbidden)."""
    headers_a, principal_a = await _create_user_session(client)
    headers_b, principal_b = await _create_user_session(client)
    assert principal_a != principal_b

    prop_id, _ = await _setup_soft_approval(owner_principal=principal_a)

    res = await client.post(
        f"/api/v1/proposals/{prop_id}/approval-token",
        json={"proposal_id": prop_id},
        headers=headers_b,
    )
    assert res.status_code == 403
    assert "Forbidden" in res.json()["detail"]


async def test_cross_principal_consume_token_rejected(client: httpx.AsyncClient) -> None:
    """Principal B cannot consume an approval token for Principal A's mandate (403 Forbidden)."""
    headers_a, principal_a = await _create_user_session(client)
    headers_b, principal_b = await _create_user_session(client)
    assert principal_a != principal_b

    prop_id, _ = await _setup_soft_approval(owner_principal=principal_a)

    # Principal A issues token legitimately
    issue_res = await client.post(
        f"/api/v1/proposals/{prop_id}/approval-token",
        json={"proposal_id": prop_id},
        headers=headers_a,
    )
    assert issue_res.status_code == 200
    token_meta = issue_res.json()

    # Principal B attempts consumption
    consume_res = await client.post(
        f"/api/v1/proposals/{prop_id}/approve",
        json={
            "proposal_id": prop_id,
            "token": token_meta["token"],
            "issued_at": token_meta["issued_at"],
            "nonce": token_meta["nonce"],
        },
        headers=headers_b,
    )
    assert consume_res.status_code == 403
    assert "Forbidden" in consume_res.json()["detail"]


async def test_ownerless_legacy_mandate_fails_closed(client: httpx.AsyncClient) -> None:
    """Legacy mandates without an owner (owner_user_id IS NULL) fail closed for approvals."""
    # Create legacy mandate with owner_user_id = None
    prop_id, mandate_id = await _setup_soft_approval(owner_principal="temporary_owner")
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE mandates SET owner_user_id = NULL WHERE mandate_id = ?",
            (mandate_id,),
        )
        await db.commit()

    # Attempt token issuance by any user
    headers, _ = await _create_user_session(client)
    res = await client.post(
        f"/api/v1/proposals/{prop_id}/approval-token",
        json={"proposal_id": prop_id},
        headers=headers,
    )
    assert res.status_code == 403
    assert "Ownerless mandate" in res.json()["detail"]


async def test_client_cannot_forge_ownership_field(client: httpx.AsyncClient) -> None:
    """Client cannot supply owner_user_id in MandateCreate payload (rejected as extra field)."""
    headers, _ = await _create_user_session(client)
    res = await client.post(
        "/api/v1/mandates",
        json={
            "max_total_budget": "50.00",
            "currency": "USD",
            "allowed_merchants": ["store.com"],
            "allowed_categories": ["books"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "owner_user_id": "forged_owner_identity",
        },
        headers=headers,
    )
    assert res.status_code == 422  # extra forbidden in MandateCreate


async def test_concurrent_approval_single_use(client: httpx.AsyncClient) -> None:
    """Concurrent attempts to consume the same approval token allow only 1 winner."""
    headers, principal_id = await _create_user_session(client)
    prop_id, _ = await _setup_soft_approval(owner_principal=principal_id)

    issue_res = await client.post(
        f"/api/v1/proposals/{prop_id}/approval-token",
        json={"proposal_id": prop_id},
        headers=headers,
    )
    assert issue_res.status_code == 200
    token_meta = issue_res.json()

    payload = {
        "proposal_id": prop_id,
        "token": token_meta["token"],
        "issued_at": token_meta["issued_at"],
        "nonce": token_meta["nonce"],
    }

    # Execute 5 concurrent consumption requests
    async def _try_consume():
        return await client.post(
            f"/api/v1/proposals/{prop_id}/approve",
            json=payload,
            headers=headers,
        )

    responses = await asyncio.gather(*[_try_consume() for _ in range(5)])
    status_codes = [r.status_code for r in responses]

    # Exactly one must succeed (200), remaining 4 must fail (400 replay detected)
    assert status_codes.count(200) == 1
    assert status_codes.count(400) == 4
