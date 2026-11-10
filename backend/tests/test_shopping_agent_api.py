"""Authenticated API boundaries for the Shopping Agent."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.api.v1 import shopping_agent as shopping_agent_api
from app.schemas.mandate import MandateCreate
from app.schemas.shopping_agent import ShoppingAgentRequest
from app.services.gemini_client import GeminiProviderError, GeminiRateLimitError
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


async def test_provider_diagnostics_are_logged_but_not_returned_to_client(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_safely(*_args: object, **_kwargs: object) -> None:
        raise GeminiProviderError(
            "provider body contains private detail",
            request_id="safe-correlation-id",
            stage="after_tool_response",
            category="INVALID_ARGUMENT",
            provider_http_status=400,
            provider_code=400,
        )

    monkeypatch.setattr(shopping_agent_api, "run_shopping_agent", fail_safely)
    caplog.set_level(logging.WARNING, logger=shopping_agent_api.__name__)

    with pytest.raises(shopping_agent_api.HTTPException) as error:
        await shopping_agent_api.chat(
            ShoppingAgentRequest(message="Find a bottle."),
            session=SimpleNamespace(user_id="readonly-user"),
        )

    assert error.value.status_code == 502
    assert error.value.detail == "Shopping Agent provider is unavailable."
    assert "provider body contains private detail" not in error.value.detail
    assert "provider body contains private detail" not in caplog.text
    assert "safe-correlation-id" in caplog.text
    assert "after_tool_response" in caplog.text
    assert "INVALID_ARGUMENT" in caplog.text
    assert "provider_http_status=400" in caplog.text


async def test_rate_limit_produces_429_and_logs_sanitized_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_rate_limited(*_args: object, **_kwargs: object) -> None:
        raise GeminiRateLimitError(
            "secret quota or provider detail",
            request_id="rate-limit-correlation-id",
            stage="initial_generation",
            category="RESOURCE_EXHAUSTED",
            provider_http_status=429,
            provider_code=429,
        )

    monkeypatch.setattr(shopping_agent_api, "run_shopping_agent", fail_rate_limited)
    caplog.set_level(logging.WARNING, logger=shopping_agent_api.__name__)

    with pytest.raises(shopping_agent_api.HTTPException) as error:
        await shopping_agent_api.chat(
            ShoppingAgentRequest(message="Find a bottle."),
            session=SimpleNamespace(user_id="readonly-user"),
        )

    assert error.value.status_code == 429
    assert error.value.detail == "Shopping Agent is busy. Try again shortly."
    assert "secret quota or provider detail" not in error.value.detail
    assert "secret quota or provider detail" not in caplog.text
    assert "rate-limit-correlation-id" in caplog.text
    assert "initial_generation" in caplog.text
    assert "RESOURCE_EXHAUSTED" in caplog.text
    assert "provider_http_status=429" in caplog.text
    assert "provider_code=429" in caplog.text
