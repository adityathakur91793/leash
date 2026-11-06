"""Authenticated API boundaries for the Shopping Agent."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.schemas.mandate import MandateCreate
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)

pytestmark = pytest.mark.asyncio


async def test_shopping_agent_mandates_require_a_valid_session(client: httpx.AsyncClient) -> None:
    missing = await client.get("/api/v1/shopping-agent/mandates")
    invalid = await client.get(
        "/api/v1/shopping-agent/mandates",
        headers={"Authorization": "Bearer invalid-session-token"},
    )
    assert missing.status_code == 401
    assert invalid.status_code == 401


async def test_mandate_choices_are_limited_to_current_session_owner(
    client: httpx.AsyncClient,
) -> None:
    session = await client.post("/api/v1/sessions", json={})
    token = session.json()["session_token"]
    user_id = session.json()["user_id"]
    owned = await create_draft_mandate(
        MandateCreate(
            max_total_budget=Decimal("50.00"),
            currency="USD",
            allowed_merchants=["trailmark.example"],
            allowed_categories=["outdoors"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        ),
        owner_user_id=user_id,
    )
    await confirm_mandate(owned.mandate_id)
    await sign_mandate(owned.mandate_id)
    await activate_mandate(owned.mandate_id)
    other = await create_draft_mandate(
        MandateCreate(
            max_total_budget=Decimal("50.00"),
            currency="USD",
            allowed_merchants=["trailmark.example"],
            allowed_categories=["outdoors"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        ),
        owner_user_id="another-principal",
    )
    response = await client.get(
        "/api/v1/shopping-agent/mandates",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert [item["mandate_id"] for item in response.json()] == [owned.mandate_id]
    assert other.mandate_id not in {item["mandate_id"] for item in response.json()}


async def test_agent_chat_requires_session_and_caps_message(client: httpx.AsyncClient) -> None:
    missing = await client.post("/api/v1/shopping-agent/chat", json={"message": "hello"})
    assert missing.status_code == 401
    session = await client.post("/api/v1/sessions", json={})
    token = session.json()["session_token"]
    too_long = await client.post(
        "/api/v1/shopping-agent/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "x" * 1501},
    )
    assert too_long.status_code == 422


async def test_agent_chat_rejects_oversized_raw_body(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/shopping-agent/chat",
        content=b"{" + b" " * 9000 + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
