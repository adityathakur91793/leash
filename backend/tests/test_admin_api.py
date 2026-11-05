"""Tests for Admin API endpoints: authentication, kill-switch, audit logs, and verification."""

import httpx
import pytest

from app.core.config import settings

ADMIN_KEY = "leash-test-admin-key-32-bytes-minimum-secret!!"


@pytest.mark.asyncio
async def test_admin_endpoints_require_admin_header(client: httpx.AsyncClient) -> None:
    """Missing X-Admin-Key header returns 422 Unprocessable Entity."""
    response = await client.get("/api/v1/admin/kill-switch")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_endpoints_reject_invalid_key(client: httpx.AsyncClient) -> None:
    """Invalid X-Admin-Key header returns 403 Forbidden."""
    headers = {"X-Admin-Key": "wrong-secret-key-that-does-not-match!!"}
    response = await client.get("/api/v1/admin/kill-switch", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid admin API key."


@pytest.mark.asyncio
async def test_admin_endpoints_return_503_when_unconfigured(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ADMIN_API_KEY is not configured or < 32 chars, endpoint fails closed with 503."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    headers = {"X-Admin-Key": "some-arbitrary-key"}
    response = await client.get("/api/v1/admin/kill-switch", headers=headers)
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


@pytest.mark.asyncio
async def test_admin_kill_switch_lifecycle(client: httpx.AsyncClient) -> None:
    """Engage and disengage kill-switch via admin API."""
    headers = {"X-Admin-Key": ADMIN_KEY}

    # 1. Initial status is inactive
    status_resp = await client.get("/api/v1/admin/kill-switch", headers=headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["kill_switch_active"] is False

    # 2. Engage
    engage_payload = {
        "engaged_by": "sec-ops-admin",
        "reason": "Suspicious anomaly detected across payment rails.",
    }
    engage_resp = await client.post(
        "/api/v1/admin/kill-switch/engage",
        headers=headers,
        json=engage_payload,
    )
    assert engage_resp.status_code == 200
    assert engage_resp.json()["kill_switch_active"] is True
    assert engage_resp.json()["engaged_by"] == "sec-ops-admin"
    assert "Suspicious anomaly" in engage_resp.json()["reason"]

    # 3. Disengage
    disengage_payload = {
        "disengaged_by": "sec-ops-admin",
        "reason": "Anomaly investigated and resolved; clearing lockdown.",
    }
    disengage_resp = await client.post(
        "/api/v1/admin/kill-switch/disengage",
        headers=headers,
        json=disengage_payload,
    )
    assert disengage_resp.status_code == 200
    assert disengage_resp.json()["kill_switch_active"] is False


@pytest.mark.asyncio
async def test_admin_kill_switch_input_validation(client: httpx.AsyncClient) -> None:
    """Reject engage/disengage with empty or too-short reasons."""
    headers = {"X-Admin-Key": ADMIN_KEY}

    # Reason too short (< 3 chars)
    resp = await client.post(
        "/api/v1/admin/kill-switch/engage",
        headers=headers,
        json={"engaged_by": "admin", "reason": "no"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_admin_audit_logs_pagination_and_query(client: httpx.AsyncClient) -> None:
    """Retrieve audit logs with pagination and verify structure."""
    headers = {"X-Admin-Key": ADMIN_KEY}

    response = await client.get("/api/v1/admin/audit/logs?limit=10&offset=0", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "total" in data
    assert "items" in data
    assert data["limit"] == 10
    assert data["offset"] == 0
    assert len(data["items"]) >= 1

    # First entry should be GENESIS
    genesis = data["items"][0]
    assert genesis["sequence_number"] == 1
    assert genesis["event_type"] == "GENESIS"
    assert genesis["prev_hash"] == "0" * 64
    assert len(genesis["entry_hash"]) == 64


@pytest.mark.asyncio
async def test_admin_audit_logs_limit_bounds(client: httpx.AsyncClient) -> None:
    """Limit > 100 must be rejected with 422."""
    headers = {"X-Admin-Key": ADMIN_KEY}
    response = await client.get("/api/v1/admin/audit/logs?limit=200", headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_audit_log_by_sequence(client: httpx.AsyncClient) -> None:
    """Retrieve individual audit log record by sequence number."""
    headers = {"X-Admin-Key": ADMIN_KEY}

    # Valid sequence 1
    resp = await client.get("/api/v1/admin/audit/logs/1", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["sequence_number"] == 1

    # Non-existent sequence
    resp_404 = await client.get("/api/v1/admin/audit/logs/99999", headers=headers)
    assert resp_404.status_code == 404


@pytest.mark.asyncio
async def test_admin_audit_verify(client: httpx.AsyncClient) -> None:
    """Cryptographic chain verification endpoint returns valid status."""
    headers = {"X-Admin-Key": ADMIN_KEY}

    resp = await client.get("/api/v1/admin/audit/verify", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["total_records"] >= 1
    assert data["error_message"] is None


@pytest.mark.asyncio
async def test_admin_audit_checkpoints(client: httpx.AsyncClient) -> None:
    """List checkpoints endpoint returns genesis checkpoint."""
    headers = {"X-Admin-Key": ADMIN_KEY}

    resp = await client.get("/api/v1/admin/audit/checkpoints", headers=headers)
    assert resp.status_code == 200
    checkpoints = resp.json()
    assert isinstance(checkpoints, list)
    assert len(checkpoints) >= 1
    genesis_cp = checkpoints[0]
    assert genesis_cp["sequence_number"] == 1
    assert len(genesis_cp["checkpoint_sig"]) == 64
