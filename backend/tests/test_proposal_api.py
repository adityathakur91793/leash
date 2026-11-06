"""API integration tests for purchase proposal endpoints."""

from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
import pytest

from app.db.database import get_db_path

pytestmark = pytest.mark.asyncio


def _mandate_api_payload() -> dict:
    return {
        "max_total_budget": "40.00",
        "currency": "USD",
        "allowed_merchants": ["amazon.com"],
        "allowed_categories": ["books"],
        "max_item_count": 2,
        "max_quantity_per_item": 1,
        "max_price_per_item": "25.00",
        "allow_subscriptions": False,
        "require_user_approval": True,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    }


def _proposal_api_payload(mandate_id: str, **overrides: object) -> dict:
    payload = {
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
        "idempotency_key": "api-req-001",
    }
    payload.update(overrides)
    return payload


async def _create_mandate(client: httpx.AsyncClient) -> str:
    session_res = await client.post("/api/v1/sessions", json={})
    token = session_res.json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post("/api/v1/mandates", json=_mandate_api_payload(), headers=headers)
    assert response.status_code == 201
    return response.json()["mandate_id"]


async def test_api_create_proposal_success(client: httpx.AsyncClient) -> None:
    """POST /proposals creates a submitted proposal with server totals."""
    mandate_id = await _create_mandate(client)
    response = await client.post(
        "/api/v1/proposals", json=_proposal_api_payload(mandate_id)
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "SUBMITTED"
    assert data["subtotal"] == "12.50"
    assert data["total"] == "16.00"
    assert data["proposal_id"].startswith("prp_")
    assert data["mandate_id"] == mandate_id


async def test_api_rejects_client_totals_and_unexpected_fields(
    client: httpx.AsyncClient,
) -> None:
    """Clients cannot supply totals or authorization fields."""
    mandate_id = await _create_mandate(client)
    payload = _proposal_api_payload(mandate_id)
    payload["total"] = "1.00"
    response = await client.post("/api/v1/proposals", json=payload)
    assert response.status_code == 422


async def test_api_invalid_price_quantity_currency(client: httpx.AsyncClient) -> None:
    """Invalid money, quantity, and currency values are rejected."""
    mandate_id = await _create_mandate(client)

    negative = _proposal_api_payload(mandate_id, unit_price="-1.00")
    assert (await client.post("/api/v1/proposals", json=negative)).status_code == 422

    qty = _proposal_api_payload(mandate_id, quantity=0)
    assert (await client.post("/api/v1/proposals", json=qty)).status_code == 422

    currency = _proposal_api_payload(mandate_id, currency="usd")
    assert (await client.post("/api/v1/proposals", json=currency)).status_code == 422


async def test_api_missing_mandate(client: httpx.AsyncClient) -> None:
    """POST /proposals returns 404 when the mandate does not exist."""
    response = await client.post(
        "/api/v1/proposals", json=_proposal_api_payload("man_missing")
    )
    assert response.status_code == 404


async def test_api_invalid_merchant(client: httpx.AsyncClient) -> None:
    """Malformed merchant identifiers are rejected."""
    mandate_id = await _create_mandate(client)
    payload = _proposal_api_payload(mandate_id, merchant="https://amazon.com")
    response = await client.post("/api/v1/proposals", json=payload)
    assert response.status_code == 422


async def test_api_duplicate_proposal_replay(client: httpx.AsyncClient) -> None:
    """Duplicate submissions return the original proposal without inserting."""
    mandate_id = await _create_mandate(client)
    payload = _proposal_api_payload(mandate_id)
    first = await client.post("/api/v1/proposals", json=payload)
    second = await client.post("/api/v1/proposals", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["proposal_id"] == second.json()["proposal_id"]

    listed = await client.get(f"/api/v1/proposals?mandate_id={mandate_id}")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


async def test_api_idempotency_conflict(client: httpx.AsyncClient) -> None:
    """The same idempotency key cannot cover two different baskets."""
    mandate_id = await _create_mandate(client)
    first = _proposal_api_payload(mandate_id, product_id="sku-1")
    second = _proposal_api_payload(mandate_id, product_id="sku-2")
    assert (await client.post("/api/v1/proposals", json=first)).status_code == 201
    conflict = await client.post("/api/v1/proposals", json=second)
    assert conflict.status_code == 409


async def test_api_get_and_list_proposals(client: httpx.AsyncClient) -> None:
    """GET by ID and list endpoints return persisted proposals."""
    mandate_id = await _create_mandate(client)
    created = await client.post(
        "/api/v1/proposals", json=_proposal_api_payload(mandate_id)
    )
    proposal_id = created.json()["proposal_id"]

    fetched = await client.get(f"/api/v1/proposals/{proposal_id}")
    assert fetched.status_code == 200
    assert fetched.json()["product_id"] == "sku-100"

    missing = await client.get("/api/v1/proposals/prp_missing")
    assert missing.status_code == 404

    listed = await client.get("/api/v1/proposals?limit=10")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


async def test_api_tampered_proposal_get(client: httpx.AsyncClient) -> None:
    """GET rejects proposals whose stored terms no longer match the hash."""
    mandate_id = await _create_mandate(client)
    created = await client.post(
        "/api/v1/proposals", json=_proposal_api_payload(mandate_id)
    )
    proposal_id = created.json()["proposal_id"]

    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE proposals SET total = ? WHERE proposal_id = ?",
            ("999.00", proposal_id),
        )
        await db.commit()

    response = await client.get(f"/api/v1/proposals/{proposal_id}")
    assert response.status_code == 422


async def test_api_mandate_endpoints_still_work(client: httpx.AsyncClient) -> None:
    """Proposal routes do not break existing mandate API behavior."""
    mandate_id = await _create_mandate(client)
    await client.post("/api/v1/proposals", json=_proposal_api_payload(mandate_id))
    confirm = await client.post(f"/api/v1/mandates/{mandate_id}/confirm")
    assert confirm.status_code == 200
    sign = await client.post(f"/api/v1/mandates/{mandate_id}/sign")
    assert sign.status_code == 200
    activate = await client.post(f"/api/v1/mandates/{mandate_id}/activate")
    assert activate.status_code == 200
    verify = await client.get(f"/api/v1/mandates/{mandate_id}/verify")
    assert verify.status_code == 200
    assert verify.json()["valid"] is True
