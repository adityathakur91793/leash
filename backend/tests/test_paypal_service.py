"""Tests for PayPalClient in Phase 3 PayPal Sandbox Executor."""

from datetime import datetime, timezone
from decimal import Decimal
import json

import httpx
import pytest

from app.core.config import settings
from app.schemas.mandate import Mandate, MandateStatus
from app.schemas.proposal import Proposal, ProposalStatus
from app.services.paypal_service import (
    PayPalAuthError,
    PayPalCaptureError,
    PayPalClient,
    PayPalConfigError,
    PayPalNetworkError,
    PayPalOrderError,
)

_OPEN_CLIENTS: list[httpx.AsyncClient] = []


@pytest.fixture(autouse=True)
async def close_mock_clients():
    """Close every HTTP client allocated by a service test."""
    yield
    while _OPEN_CLIENTS:
        await _OPEN_CLIENTS.pop().aclose()


def _mock_client(handler) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    _OPEN_CLIENTS.append(client)
    return client


def _make_dummy_mandate() -> Mandate:
    return Mandate(
        mandate_id="man_test_12345",
        status=MandateStatus.ACTIVE,
        max_total_budget=Decimal("500.00"),
        currency="USD",
        allowed_merchants=["vendor-a.com"],
        allowed_categories=["electronics"],
        created_at=datetime.now(timezone.utc),
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        signature="dummy_sig",
        signed_at=datetime.now(timezone.utc),
        signature_algorithm="HMAC-SHA256",
        owner_user_id="usr_owner_1",
    )


def _make_dummy_proposal() -> Proposal:
    return Proposal(
        proposal_id="prop_test_12345",
        mandate_id="man_test_12345",
        merchant="vendor-a.com",
        category="electronics",
        product_name="USB-C Cable",
        product_id="prod_01",
        quantity=2,
        unit_price=Decimal("15.50"),
        currency="USD",
        subtotal=Decimal("31.00"),
        tax=Decimal("2.48"),
        shipping=Decimal("5.00"),
        other_charges=Decimal("0.00"),
        total=Decimal("38.48"),
        product_source="direct",
        status=ProposalStatus.APPROVED,
        created_at=datetime.now(timezone.utc),
        fingerprint="dummy_fp",
        content_hash="dummy_hash",
    )


@pytest.mark.asyncio
async def test_paypal_client_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing client ID or secret raises PayPalConfigError."""
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "")
    client = PayPalClient()
    with pytest.raises(PayPalConfigError):
        await client.get_access_token()


def test_paypal_client_rejects_non_sandbox_api_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials can never be sent to a live or caller-controlled endpoint."""
    monkeypatch.setattr(settings, "PAYPAL_SANDBOX_BASE_URL", "https://api.paypal.com")
    with pytest.raises(PayPalConfigError, match="official Sandbox host"):
        PayPalClient()._get_base_url()


@pytest.mark.asyncio
async def test_paypal_client_rejects_untrusted_return_url() -> None:
    """Checkout return/cancel links cannot redirect users to arbitrary sites."""
    paypal = PayPalClient()
    with pytest.raises(ValueError, match="approved LEASH frontend origin"):
        await paypal.create_order(
            proposal=_make_dummy_proposal(),
            mandate=_make_dummy_mandate(),
            paypal_request_id="order-return-url",
            return_url="https://attacker.example/steal",
        )


@pytest.mark.asyncio
async def test_paypal_client_token_success_and_caching() -> None:
    """OAuth token request succeeds and is cached until near expiration."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        assert request.url.path == "/v1/oauth2/token"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={"access_token": "mock_access_token_xyz", "expires_in": 3600},
        )

    mock_client = _mock_client(handler)
    paypal = PayPalClient(client=mock_client)

    token1 = await paypal.get_access_token()
    assert token1 == "mock_access_token_xyz"
    assert call_count == 1

    # Second call should use cached token without additional network request
    token2 = await paypal.get_access_token()
    assert token2 == "mock_access_token_xyz"
    assert call_count == 1


@pytest.mark.asyncio
async def test_paypal_client_token_failure() -> None:
    """Non-200 OAuth response raises PayPalAuthError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    mock_client = _mock_client(handler)
    paypal = PayPalClient(client=mock_client)

    with pytest.raises(PayPalAuthError):
        await paypal.get_access_token()


@pytest.mark.asyncio
async def test_paypal_client_create_order_success() -> None:
    """Create order sends exact decimal breakdown and extracts sandbox approval URL."""
    mandate = _make_dummy_mandate()
    proposal = _make_dummy_proposal()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token123", "expires_in": 3600})
        if request.url.path == "/v2/checkout/orders":
            assert request.headers.get("PayPal-Request-Id") == "ord_req_test"
            assert request.headers.get("Authorization") == "Bearer token123"
            body = json.loads(request.content.decode())
            amount = body["purchase_units"][0]["amount"]
            assert amount["currency_code"] == "USD"
            assert amount["value"] == "38.48"
            assert amount["breakdown"]["item_total"]["value"] == "31.00"
            assert amount["breakdown"]["tax_total"]["value"] == "2.48"
            assert amount["breakdown"]["shipping"]["value"] == "5.00"
            return httpx.Response(
                201,
                json={
                    "id": "PAYPAL_ORDER_999",
                    "status": "CREATED",
                    "links": [
                        {
                            "rel": "approve",
                            "href": "https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL_ORDER_999",
                        }
                    ],
                },
            )
        return httpx.Response(404)

    mock_client = _mock_client(handler)
    paypal = PayPalClient(client=mock_client)

    res = await paypal.create_order(
        proposal=proposal,
        mandate=mandate,
        paypal_request_id="ord_req_test",
    )

    assert res["order_id"] == "PAYPAL_ORDER_999"
    assert res["status"] == "CREATED"
    assert "https://www.sandbox.paypal.com" in res["approval_url"]


@pytest.mark.asyncio
async def test_paypal_client_rejects_untrusted_approval_url() -> None:
    """Untrusted host or insecure scheme in approval link raises PayPalOrderError."""
    mandate = _make_dummy_mandate()
    proposal = _make_dummy_proposal()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token123", "expires_in": 3600})
        if request.url.path == "/v2/checkout/orders":
            return httpx.Response(
                201,
                json={
                    "id": "PAYPAL_ORDER_BAD",
                    "status": "CREATED",
                    "links": [
                        {"rel": "approve", "href": "https://malicious-phishing.com/checkout"}
                    ],
                },
            )
        return httpx.Response(404)

    mock_client = _mock_client(handler)
    paypal = PayPalClient(client=mock_client)

    with pytest.raises(PayPalOrderError, match="not a permitted PayPal Sandbox host"):
        await paypal.create_order(
            proposal=proposal,
            mandate=mandate,
            paypal_request_id="ord_req_bad",
        )


@pytest.mark.asyncio
async def test_paypal_client_get_order_and_capture() -> None:
    """get_order and capture_order interact properly with Orders API."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token123", "expires_in": 3600})
        if request.url.path == "/v2/checkout/orders/ORD_123" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "ORD_123",
                    "status": "APPROVED",
                    "purchase_units": [
                        {"amount": {"currency_code": "USD", "value": "38.48"}}
                    ],
                },
            )
        if request.url.path == "/v2/checkout/orders/ORD_123/capture" and request.method == "POST":
            assert request.headers.get("PayPal-Request-Id") == "cap_req_1"
            return httpx.Response(
                201,
                json={
                    "id": "ORD_123",
                    "status": "COMPLETED",
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {"id": "CAP_98765", "status": "COMPLETED"}
                                ]
                            }
                        }
                    ],
                },
            )
        return httpx.Response(404)

    mock_client = _mock_client(handler)
    paypal = PayPalClient(client=mock_client)

    order_info = await paypal.get_order("ORD_123")
    assert order_info["status"] == "APPROVED"

    capture_res = await paypal.capture_order("ORD_123", capture_request_id="cap_req_1")
    assert capture_res["capture_id"] == "CAP_98765"
    assert capture_res["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_paypal_client_capture_failure() -> None:
    """Failure response on capture raises PayPalCaptureError."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token123", "expires_in": 3600})
        if request.url.path == "/v2/checkout/orders/ORD_FAIL/capture":
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
        return httpx.Response(404)

    mock_client = _mock_client(handler)
    paypal = PayPalClient(client=mock_client)

    with pytest.raises(PayPalCaptureError):
        await paypal.capture_order("ORD_FAIL", capture_request_id="req_fail")


@pytest.mark.asyncio
async def test_paypal_client_network_error() -> None:
    """Network connection timeout raises PayPalNetworkError."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Network unreachable")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    paypal = PayPalClient(client=mock_client)

    with pytest.raises(PayPalNetworkError):
        await paypal.get_access_token()
