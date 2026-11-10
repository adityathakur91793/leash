"""Smoke tests for the local-only Phase 6 Attack Lab harness."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest

from app.schemas.intent_audit import (
    IntentAuditorInput,
    IntentAuditorPolicyRule,
    IntentMandateSummary,
    IntentProposalSummary,
    IntentRiskLevel,
)
from app.services.gemini_client import GeminiClient
from app.services.gemini_client import GeminiProviderError
from app.services.intent_auditor_client import IntentAuditorClient
from app.services.paypal_service import PayPalNetworkError
from app.services.paypal_service import PayPalClient
from app.services import policy_engine
from tests.attack_lab.support import (
    AttackCase,
    AttackClock,
    AsyncBarrier,
    BlockedDestinations,
    ExternalNetworkBlocked,
    ScriptedHTTPX,
    ScriptedPayPal,
    safe_http_evidence,
)

pytestmark = pytest.mark.asyncio


async def test_synthetic_case_is_per_test_owned_and_auditable(
    attack_case: AttackCase,
    attack_database: Path,
    verify_attack_audit_chain: Callable[[], Awaitable[Any]],
) -> None:
    assert attack_database.exists()
    assert attack_database.name == "attack-lab.sqlite3"
    assert attack_case.mandate_a.owner_user_id == attack_case.user_a.user_id
    assert attack_case.mandate_b.owner_user_id == attack_case.user_b.user_id
    assert attack_case.proposal_a.mandate_id == attack_case.mandate_a.mandate_id
    assert attack_case.proposal_b.mandate_id == attack_case.mandate_b.mandate_id

    audit_result = await verify_attack_audit_chain()
    assert audit_result.valid is True


async def test_gemini_script_records_three_tools_and_model_tool_call(
    attack_gemini_client: tuple[GeminiClient, ScriptedHTTPX],
) -> None:
    client, script = attack_gemini_client
    script.queue_response(httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{
            "functionCall": {
                "name": "search_products",
                "args": {"keyword": "synthetic item"},
            }
        }]}}]},
    ))

    response = await client.generate([{"role": "user", "parts": [{"text": "find an item"}]}])

    assert response["candidates"]
    assert script.request_count == 1
    assert script.requested_tools == [(
        "search_products", "get_product", "propose_purchase"
    )]
    assert script.model_tool_calls == [("search_products",)]
    assert all(call[0] not in {"approve", "capture_payment"}
               for call in script.model_tool_calls)


@pytest.mark.parametrize(
    "scripted_response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(503, text="synthetic provider failure"),
        httpx.ReadTimeout("scripted provider timeout"),
    ],
    ids=("malformed", "provider-error", "timeout"),
)
async def test_gemini_script_supports_failure_modes(
    attack_gemini_client: tuple[GeminiClient, ScriptedHTTPX],
    scripted_response: httpx.Response | Exception,
) -> None:
    client, script = attack_gemini_client
    script.queue_response(scripted_response)

    with pytest.raises(GeminiProviderError):
        await client.generate([])

    assert script.request_count == 1


async def test_intent_auditor_mock_records_no_tools(
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
) -> None:
    client, script = attack_intent_client
    payload = IntentAuditorInput(
        mandate=IntentMandateSummary(
            currency="USD",
            max_total_budget="100.00",
            max_price_per_item="50.00",
            max_item_count=3,
            max_quantity_per_item=2,
            allowed_merchants=["synthetic-shop.example"],
            allowed_categories=["test-goods"],
            allow_subscriptions=False,
            require_user_approval=True,
            is_active_and_unexpired=True,
        ),
        proposal=IntentProposalSummary(
            product_name="Synthetic item",
            merchant="synthetic-shop.example",
            category="test-goods",
            quantity=1,
            unit_price="12.50",
            total="16.00",
            currency="USD",
        ),
        deterministic_outcome="ALLOW",
        policy_rules=[
            IntentAuditorPolicyRule(rule_id=f"P{number:02d}", status="PASS")
            for number in range(1, 13)
        ],
    )
    script.queue_response(httpx.Response(
        200,
        json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "schema_version": "1.0",
                        "risk_level": "LOW",
                        "finding_codes": [],
                        "affected_fields": [],
                    }),
                }
            }]
        },
    ))

    result = await client.assess(payload)

    assert result.risk_level == IntentRiskLevel.LOW
    assert script.request_count == 1
    assert script.requested_tools == [()]
    assert script.model_tool_calls == [()]


async def test_paypal_script_models_order_approval_and_capture(
    attack_case: AttackCase,
    attack_paypal_client: tuple[PayPalClient, ScriptedPayPal],
) -> None:
    client, script = attack_paypal_client
    created = await client.create_order(
        proposal=attack_case.proposal_a,
        mandate=attack_case.mandate_a,
        paypal_request_id="attack-lab-order-request",
    )
    order = await client.get_order(created["order_id"])
    captured = await client.capture_order(
        created["order_id"], capture_request_id="attack-lab-capture-request"
    )

    assert created["approval_url"].startswith(
        "https://www.sandbox.paypal.com/"
    )
    assert order["status"] == "APPROVED"
    assert captured["status"] == "COMPLETED"
    assert captured["capture_id"] == "ATTACK-LAB-CAPTURE-1"
    assert script.capture_attempt_count == 1
    assert script.capture_request_ids == ["attack-lab-capture-request"]
    assert {record.host for record in script.requests} == {
        "api-m.sandbox.paypal.com"
    }


async def test_paypal_script_can_reconcile_an_ambiguous_capture(
    attack_paypal_client: tuple[PayPalClient, ScriptedPayPal],
) -> None:
    client, script = attack_paypal_client
    script.queue_response(
        "POST",
        "/v2/checkout/orders/ATTACK-LAB-ORDER-AMBIG/capture",
        httpx.ReadTimeout("scripted timeout"),
    )

    with pytest.raises(PayPalNetworkError):
        await client.capture_order(
            "ATTACK-LAB-ORDER-AMBIG",
            capture_request_id="attack-lab-ambiguous-request",
        )

    assert script.capture_attempt_count == 1
    assert script.capture_request_ids == ["attack-lab-ambiguous-request"]


async def test_unmocked_external_httpx_destinations_are_blocked(
    attack_lab_safety: BlockedDestinations,
) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ExternalNetworkBlocked):
            await client.get("https://no-network.attack-lab.invalid/path")

    assert attack_lab_safety.hosts == ["no-network.attack-lab.invalid"]
    with pytest.raises(ExternalNetworkBlocked):
        httpx.AsyncClient(transport=httpx.AsyncHTTPTransport())


@pytest.mark.parametrize("client_type", [httpx.Client, httpx.AsyncClient])
async def test_httpx_constructor_rejects_explicit_proxies(client_type: Any) -> None:
    with pytest.raises(ExternalNetworkBlocked, match="proxy configuration"):
        client_type(proxy="http://proxy.attack-lab.invalid:8080")


@pytest.mark.parametrize(
    ("client_type", "transport"),
    [
        (httpx.Client, httpx.HTTPTransport()),
        (httpx.AsyncClient, httpx.AsyncHTTPTransport()),
    ],
)
async def test_httpx_constructor_rejects_host_specific_mounts(
    client_type: Any,
    transport: Any,
) -> None:
    mounts = {"https://no-network.attack-lab.invalid": transport}
    with pytest.raises(ExternalNetworkBlocked, match="mount configuration"):
        client_type(mounts=mounts)


async def test_approved_in_process_httpx_transports_still_work() -> None:
    async def asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"local-asgi"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://attack-lab.local",
    ) as asgi_client:
        asgi_response = await asgi_client.get("/")

    sync_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=f"mock:{request.url.host}")
    )
    with httpx.Client(transport=sync_transport) as sync_client:
        sync_response = sync_client.get("https://mock.attack-lab.invalid/")

    assert asgi_response.text == "local-asgi"
    assert sync_response.text == "mock:mock.attack-lab.invalid"


async def test_clock_barrier_and_evidence_helpers_are_deterministic(
    attack_clock: AttackClock,
    attack_barrier: type[AsyncBarrier],
    attack_case: AttackCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attack_clock.install(monkeypatch, [policy_engine])
    first = policy_engine.datetime.now()
    attack_clock.advance(timedelta(minutes=5))
    assert policy_engine.datetime.now() - first == timedelta(minutes=5)

    barrier = attack_barrier(2)
    reached: list[str] = []

    async def participant(name: str) -> None:
        await barrier.wait()
        reached.append(name)

    await asyncio.gather(participant("a"), participant("b"))
    assert reached == ["a", "b"] or reached == ["b", "a"]

    evidence = safe_http_evidence(httpx.Response(
        200,
        json={"session_token": attack_case.user_a.session_token, "status": "ok"},
    ))
    assert evidence == {
        "status_code": 200,
        "body": {"session_token": "<redacted>", "status": "ok"},
    }
