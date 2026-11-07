"""API integration tests for the Mandate REST endpoints."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import aiosqlite

from app.db.database import get_db_path

pytestmark = pytest.mark.asyncio


def _sample_api_payload() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "max_total_budget": "40.00",
        "currency": "USD",
        "allowed_merchants": ["amazon.com", "target.com"],
        "allowed_categories": ["books", "gifts"],
        "max_item_count": 2,
        "max_quantity_per_item": 1,
        "max_price_per_item": "25.00",
        "allow_subscriptions": False,
        "require_user_approval": True,
        "expires_at": (now + timedelta(hours=24)).isoformat(),
    }


async def _get_auth_headers(
    client: httpx.AsyncClient,
) -> dict[str, str]:
    resp = await client.post("/api/v1/sessions", json={})
    token = resp.json()["session_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_api_create_mandate_requires_auth(client: httpx.AsyncClient) -> None:
    """Test POST /api/v1/mandates requires authenticated session."""
    payload = _sample_api_payload()
    response = await client.post("/api/v1/mandates", json=payload)
    assert response.status_code == 401


async def test_api_create_mandate_success(client: httpx.AsyncClient) -> None:
    """Test POST /api/v1/mandates creates a draft mandate."""
    headers = await _get_auth_headers(client)
    payload = _sample_api_payload()
    response = await client.post("/api/v1/mandates", json=payload, headers=headers)
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "DRAFT"
    assert data["currency"] == "USD"
    assert data["max_total_budget"] == "40.00"
    assert data["mandate_id"].startswith("man_")
    assert data["signature"] is None
    assert data["owner_user_id"] is not None
    assert data["owner_user_id"].startswith("usr_")


async def test_api_create_mandate_invalid_payload(client: httpx.AsyncClient) -> None:
    """Test POST /api/v1/mandates rejects invalid input."""
    headers = await _get_auth_headers(client)
    payload = _sample_api_payload()
    payload["max_total_budget"] = "-5.00"
    response = await client.post("/api/v1/mandates", json=payload, headers=headers)
    assert response.status_code == 422


async def test_api_create_mandate_rejects_forged_owner_user_id(
    client: httpx.AsyncClient,
) -> None:
    """The API derives ownership from the session and rejects forged identities."""
    headers = await _get_auth_headers(client)
    for field in ("owner_user_id", "user_id"):
        payload = _sample_api_payload()
        payload[field] = "usr_attacker_chosen"
        response = await client.post(
            "/api/v1/mandates", json=payload, headers=headers
        )
        assert response.status_code == 422


async def test_api_full_mandate_workflow(client: httpx.AsyncClient) -> None:
    """Test end-to-end API lifecycle: create -> confirm -> sign -> activate -> verify -> revoke."""
    headers = await _get_auth_headers(client)
    # 1. Create Draft
    create_res = await client.post(
        "/api/v1/mandates", json=_sample_api_payload(), headers=headers
    )
    assert create_res.status_code == 201
    mandate_id = create_res.json()["mandate_id"]

    # 2. Get Details
    get_res = await client.get(f"/api/v1/mandates/{mandate_id}", headers=headers)
    assert get_res.status_code == 200
    assert get_res.json()["status"] == "DRAFT"

    # 3. Attempting to sign unconfirmed draft must return 409 Conflict
    early_sign = await client.post(f"/api/v1/mandates/{mandate_id}/sign", headers=headers)
    assert early_sign.status_code == 409

    # 4. Confirm
    confirm_res = await client.post(f"/api/v1/mandates/{mandate_id}/confirm", headers=headers)
    assert confirm_res.status_code == 200
    assert confirm_res.json()["status"] == "CONFIRMED"

    # 5. Sign
    sign_res = await client.post(f"/api/v1/mandates/{mandate_id}/sign", headers=headers)
    assert sign_res.status_code == 200
    assert sign_res.json()["status"] == "SIGNED"
    assert sign_res.json()["signature"] is not None

    # 6. Activate
    activate_res = await client.post(f"/api/v1/mandates/{mandate_id}/activate", headers=headers)
    assert activate_res.status_code == 200
    assert activate_res.json()["status"] == "ACTIVE"

    # 7. Verify
    verify_res = await client.get(f"/api/v1/mandates/{mandate_id}/verify", headers=headers)
    assert verify_res.status_code == 200
    assert verify_res.json()["valid"] is True
    assert verify_res.json()["status"] == "ACTIVE"

    # 8. Revoke
    revoke_res = await client.post(f"/api/v1/mandates/{mandate_id}/revoke", headers=headers)
    assert revoke_res.status_code == 200
    assert revoke_res.json()["status"] == "REVOKED"

    # 9. Verify after revocation
    verify_after_revoke = await client.get(
        f"/api/v1/mandates/{mandate_id}/verify", headers=headers
    )
    assert verify_after_revoke.status_code == 200
    assert verify_after_revoke.json()["valid"] is False
    assert verify_after_revoke.json()["status"] == "REVOKED"


async def test_api_get_nonexistent_mandate(client: httpx.AsyncClient) -> None:
    """Test GET /api/v1/mandates/nonexistent returns 404 to authenticated callers."""
    headers = await _get_auth_headers(client)
    response = await client.get("/api/v1/mandates/man_nonexistent", headers=headers)
    assert response.status_code == 404


async def test_api_list_mandates(client: httpx.AsyncClient) -> None:
    """Test GET /api/v1/mandates lists created mandates."""
    headers = await _get_auth_headers(client)
    # Create two mandates
    await client.post("/api/v1/mandates", json=_sample_api_payload(), headers=headers)
    await client.post("/api/v1/mandates", json=_sample_api_payload(), headers=headers)

    response = await client.get("/api/v1/mandates?limit=10", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2


async def test_api_list_mandates_limit_bounds(client: httpx.AsyncClient) -> None:
    """Test GET /api/v1/mandates rejects out-of-range limit values."""
    headers = await _get_auth_headers(client)
    too_small = await client.get("/api/v1/mandates?limit=0", headers=headers)
    assert too_small.status_code == 422
    too_large = await client.get("/api/v1/mandates?limit=101", headers=headers)
    assert too_large.status_code == 422


async def test_api_confirm_non_draft_conflict(client: httpx.AsyncClient) -> None:
    """Test POST /confirm returns 409 when the mandate is not a draft."""
    headers = await _get_auth_headers(client)
    create_res = await client.post(
        "/api/v1/mandates", json=_sample_api_payload(), headers=headers
    )
    mandate_id = create_res.json()["mandate_id"]
    first = await client.post(f"/api/v1/mandates/{mandate_id}/confirm", headers=headers)
    assert first.status_code == 200
    second = await client.post(f"/api/v1/mandates/{mandate_id}/confirm", headers=headers)
    assert second.status_code == 409


async def test_api_activate_tampered_mandate(client: httpx.AsyncClient) -> None:
    """Test POST /activate returns 422 when stored permissions were altered."""
    import aiosqlite

    from app.db.database import get_db_path

    headers = await _get_auth_headers(client)
    create_res = await client.post(
        "/api/v1/mandates", json=_sample_api_payload(), headers=headers
    )
    mandate_id = create_res.json()["mandate_id"]
    await client.post(f"/api/v1/mandates/{mandate_id}/confirm", headers=headers)
    sign_res = await client.post(f"/api/v1/mandates/{mandate_id}/sign", headers=headers)
    assert sign_res.status_code == 200

    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE mandates SET max_total_budget = ? WHERE mandate_id = ?",
            ("999.00", mandate_id),
        )
        await db.commit()

    activate_res = await client.post(
        f"/api/v1/mandates/{mandate_id}/activate", headers=headers
    )
    assert activate_res.status_code == 422


async def test_mandate_api_requires_auth_for_reads_and_transitions(
    client: httpx.AsyncClient,
) -> None:
    """All mandate reads and lifecycle changes require a valid session."""
    headers = await _get_auth_headers(client)
    created = await client.post(
        "/api/v1/mandates", json=_sample_api_payload(), headers=headers
    )
    mandate_id = created.json()["mandate_id"]
    assert (await client.get("/api/v1/mandates")).status_code == 401
    assert (await client.get(f"/api/v1/mandates/{mandate_id}")).status_code == 401
    assert (await client.get(f"/api/v1/mandates/{mandate_id}/verify")).status_code == 401
    for action in ("confirm", "sign", "activate", "revoke"):
        response = await client.post(f"/api/v1/mandates/{mandate_id}/{action}")
        assert response.status_code == 401


async def test_mandate_api_hides_foreign_mandates_and_denies_all_transitions(
    client: httpx.AsyncClient,
) -> None:
    """A principal cannot read, list, verify, or transition another user's mandate."""
    owner_headers = await _get_auth_headers(client)
    other_headers = await _get_auth_headers(client)
    created = await client.post(
        "/api/v1/mandates", json=_sample_api_payload(), headers=owner_headers
    )
    mandate_id = created.json()["mandate_id"]
    async with aiosqlite.connect(get_db_path()) as db:
        before_audit_count = (await (await db.execute(
            "SELECT COUNT(*) FROM audit_logs"
        )).fetchone())[0]

    listing = await client.get("/api/v1/mandates", headers=other_headers)
    assert listing.status_code == 200
    assert mandate_id not in {item["mandate_id"] for item in listing.json()}
    for suffix in ("", "/verify"):
        foreign = await client.get(
            f"/api/v1/mandates/{mandate_id}{suffix}", headers=other_headers
        )
        missing = await client.get(
            f"/api/v1/mandates/man_nonexistent{suffix}", headers=other_headers
        )
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json()["detail"] == missing.json()["detail"]
    for action in ("confirm", "sign", "activate", "revoke"):
        foreign = await client.post(
            f"/api/v1/mandates/{mandate_id}/{action}", headers=other_headers
        )
        missing = await client.post(
            f"/api/v1/mandates/man_nonexistent/{action}", headers=other_headers
        )
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json()["detail"] == missing.json()["detail"]

    async with aiosqlite.connect(get_db_path()) as db:
        after_audit_count = (await (await db.execute(
            "SELECT COUNT(*) FROM audit_logs"
        )).fetchone())[0]
    assert after_audit_count == before_audit_count

    fetched = await client.get(f"/api/v1/mandates/{mandate_id}", headers=owner_headers)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "DRAFT"
    assert fetched.json()["signature"] is None
