"""Focused tests for the Groq AI provider integration."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.mandate import MandateCreate
from app.schemas.shopping_agent import (
    ProductRecord,
    ProposalToolResult,
    ShoppingAgentResponse,
)
from app.services.groq_client import (
    GROQ_API_URL,
    GROQ_FUNCTION_DECLARATIONS,
    GroqClient,
    GroqConfigurationError,
    GroqProviderError,
    GroqRateLimitError,
)
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.shopping_agent_service import (
    ShoppingAgentBlockedError,
    ShoppingAgentInputError,
    ShoppingAgentOwnershipError,
    run_shopping_agent,
)


class _ScriptedGroqClient:
    """Mocked Groq client executing scripted OpenAI-compatible responses."""

    provider = "groq"

    def __init__(
        self,
        responses: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.call_metadata: list[tuple[str | None, str]] = []
        self._model = model or "qwen/qwen3.8-27b"

    @property
    def model(self) -> str:
        return self._model

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        request_id: str | None = None,
        stage: str = "initial_generation",
    ) -> dict[str, Any]:
        self.requests.append(json.loads(json.dumps(messages)))
        self.call_metadata.append((request_id, stage))
        if not self.responses:
            raise RuntimeError("No more scripted responses.")
        return self.responses.pop(0)


def _groq_text_response(text: str) -> dict[str, Any]:
    """Helper to create a plain text assistant completion."""
    return {
        "id": "chatcmpl-test-001",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                },
                "finish_reason": "stop",
            }
        ],
    }


def _groq_tool_call_response(
    name: str,
    arguments: dict[str, Any] | str,
    *,
    call_id: str = "call_test_123",
) -> dict[str, Any]:
    """Helper to create a single tool-call assistant completion."""
    arg_str = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "id": "chatcmpl-test-tool",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arg_str,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


async def _create_active_mandate(user_id: str = "groq-user") -> str:
    created = await create_draft_mandate(
        MandateCreate(
            max_total_budget=Decimal("150.00"),
            currency="USD",
            allowed_merchants=["trailmark.example"],
            allowed_categories=["outdoors"],
            max_item_count=5,
            max_quantity_per_item=5,
            allow_subscriptions=False,
            require_user_approval=False,
            expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        ),
        owner_user_id=user_id,
    )
    await confirm_mandate(created.mandate_id)
    await sign_mandate(created.mandate_id)
    await activate_mandate(created.mandate_id)
    return created.mandate_id


# -------------------------------------------------------------------------
# Test Cases
# -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_groq_text_generation_without_tools() -> None:
    client = _ScriptedGroqClient([_groq_text_response("Hello, I am LEASH Shopping Agent.")])
    response = await run_shopping_agent(
        "Hi there", user_id="user-1", mandate_id=None, client=client
    )
    assert isinstance(response, ShoppingAgentResponse)
    assert response.message == "Hello, I am LEASH Shopping Agent."
    assert response.products == []
    assert response.proposal is None
    assert len(client.requests) == 1
    assert client.requests[0][0]["role"] == "system"
    assert client.requests[0][1]["role"] == "user"


@pytest.mark.asyncio
async def test_groq_tool_round_trip_search_and_get_product() -> None:
    client = _ScriptedGroqClient([
        _groq_tool_call_response("search_products", {"keyword": "bottle"}, call_id="call_s1"),
        _groq_tool_call_response("get_product", {"product_id": "demo_bottle_001"}, call_id="call_g1"),
        _groq_text_response("I found the demo water bottle for $24.99."),
    ])

    response = await run_shopping_agent(
        "Find a water bottle", user_id="user-1", mandate_id=None, client=client
    )
    assert "demo water bottle for $24.99" in response.message
    assert len(response.products) == 1
    assert response.products[0].product_id == "demo_bottle_001"
    assert len(client.requests) == 3

    # Verify tool result messages preserve tool_call_id
    r2_messages = client.requests[1]
    assert r2_messages[-1]["role"] == "tool"
    assert r2_messages[-1]["tool_call_id"] == "call_s1"
    r3_messages = client.requests[2]
    assert r3_messages[-1]["role"] == "tool"
    assert r3_messages[-1]["tool_call_id"] == "call_g1"


@pytest.mark.asyncio
async def test_groq_tool_propose_purchase_evaluates_policy_deterministically() -> None:
    mandate_id = await _create_active_mandate("groq-user")
    client = _ScriptedGroqClient([
        _groq_tool_call_response(
            "propose_purchase",
            {"product_id": "demo_bottle_001", "quantity": 1},
            call_id="call_prop_1",
        ),
    ])

    response = await run_shopping_agent(
        "Buy 1 bottle with mandate",
        user_id="groq-user",
        mandate_id=mandate_id,
        client=client,
    )
    assert response.proposal is not None
    assert isinstance(response.proposal, ProposalToolResult)
    assert response.proposal.policy_decision.decision.value == "ALLOW"
    assert response.proposal.simulated is True
    # The agent turn terminates immediately on propose_purchase — no second LLM call.
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_groq_rejects_missing_function_call_id() -> None:
    # A tool call with missing/empty id must fail closed with safe diagnostics
    client = _ScriptedGroqClient([
        {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "",
                                "type": "function",
                                "function": {
                                    "name": "search_products",
                                    "arguments": '{"keyword": "bottle"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }
    ])

    with pytest.raises(GroqProviderError) as exc_info:
        await run_shopping_agent(
            "Find a bottle", user_id="user-1", mandate_id=None, client=client
        )
    assert exc_info.value.category == "missing_function_call_id"


@pytest.mark.asyncio
async def test_groq_rejects_malformed_arguments_json() -> None:
    client = _ScriptedGroqClient([
        _groq_tool_call_response("search_products", "{not valid json", call_id="c1")
    ])

    with pytest.raises(ShoppingAgentInputError):
        await run_shopping_agent(
            "Find a bottle", user_id="user-1", mandate_id=None, client=client
        )


@pytest.mark.asyncio
async def test_groq_rejects_parallel_tool_calls_batch() -> None:
    client = _ScriptedGroqClient([
        {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "search_products", "arguments": "{}"},
                            },
                            {
                                "id": "c2",
                                "type": "function",
                                "function": {"name": "get_product", "arguments": '{"product_id": "demo_bottle_001"}'},
                            },
                        ],
                    },
                }
            ],
        }
    ])

    with pytest.raises(GroqProviderError) as exc_info:
        await run_shopping_agent(
            "Find bottles", user_id="user-1", mandate_id=None, client=client
        )
    assert exc_info.value.category == "unsupported_function_call_batch"


@pytest.mark.asyncio
async def test_groq_unconfigured_api_key_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "groq")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    with pytest.raises(GroqConfigurationError):
        await run_shopping_agent("Hello", user_id="user-1", mandate_id=None)


@pytest.mark.asyncio
async def test_groq_client_handles_401_authentication_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "Invalid API Key",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GroqClient(api_key="bad-key", client=http)
        with pytest.raises(GroqProviderError) as exc_info:
            await client.generate([{"role": "user", "content": "hi"}])
        assert exc_info.value.provider_http_status == 401
        assert exc_info.value.category in {"invalid_request_error", "invalid_api_key"}


@pytest.mark.asyncio
async def test_groq_client_handles_429_rate_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "Rate limit reached",
                    "type": "rate_limit_exceeded",
                    "code": "rate_limit_exceeded",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GroqClient(api_key="valid-key", client=http)
        with pytest.raises(GroqRateLimitError) as exc_info:
            await client.generate([{"role": "user", "content": "hi"}])
        assert exc_info.value.provider_http_status == 429
        assert exc_info.value.category == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_groq_propose_purchase_enforces_mandate_owner_isolation() -> None:
    owner_mandate_id = await _create_active_mandate("owner-user")
    client = _ScriptedGroqClient([
        _groq_tool_call_response(
            "propose_purchase",
            {"product_id": "demo_bottle_001", "quantity": 1},
            call_id="call_p1",
        )
    ])

    with pytest.raises(ShoppingAgentOwnershipError):
        await run_shopping_agent(
            "Buy bottle",
            user_id="attacker-user",
            mandate_id=owner_mandate_id,
            client=client,
        )


@pytest.mark.asyncio
async def test_groq_empty_stop_response_fails_closed() -> None:
    client = _ScriptedGroqClient([
        {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "stop",
                }
            ],
        }
    ])

    with pytest.raises(GroqProviderError) as exc_info:
        await run_shopping_agent(
            "Find a bottle", user_id="user-1", mandate_id=None, client=client
        )
    assert exc_info.value.category == "empty_stop_response"
