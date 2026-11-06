"""Tests for LEASH Session API and Authentication Lifecycle."""

import hashlib
from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
import pytest

from app.db.database import get_db_path

pytestmark = pytest.mark.asyncio


async def test_session_creation_anonymous(client: httpx.AsyncClient) -> None:
    """Anonymous user session bootstrap generates server-side principal ID."""
    response = await client.post("/api/v1/sessions", json={})
    assert response.status_code == 201
    data = response.json()
    assert "session_token" in data
    assert len(data["session_token"]) >= 43  # 32 bytes base64-encoded
    assert data["user_id"].startswith("usr_")
    assert "expires_at" in data
    assert "created_at" in data


async def test_session_creation_rejects_client_supplied_user_id(
    client: httpx.AsyncClient,
) -> None:
    """Anonymous clients cannot choose or supply a user_id (extra field forbidden)."""
    response = await client.post(
        "/api/v1/sessions", json={"user_id": "impostor_principal"}
    )
    assert response.status_code == 422  # Extra inputs are not permitted


async def test_session_creation_authenticated_multi_session(
    client: httpx.AsyncClient,
) -> None:
    """Authenticated caller presenting Bearer token can mint additional sessions for same principal."""
    # Session 1 bootstrap
    res1 = await client.post("/api/v1/sessions", json={"ttl_seconds": 3600})
    assert res1.status_code == 201
    principal_id = res1.json()["user_id"]
    token1 = res1.json()["session_token"]

    # Session 2 created with token1 in Authorization header
    res2 = await client.post(
        "/api/v1/sessions",
        json={"ttl_seconds": 3600},
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert res2.status_code == 201
    assert res2.json()["user_id"] == principal_id
    assert res2.json()["session_token"] != token1  # Fresh high-entropy token


async def test_session_token_stored_hashed_only(client: httpx.AsyncClient) -> None:
    """Raw bearer token is NEVER stored in database; only SHA-256 hash is persisted."""
    response = await client.post("/api/v1/sessions", json={})
    assert response.status_code == 201
    raw_token = response.json()["session_token"]
    expected_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    principal_id = response.json()["user_id"]

    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        # The raw token must NOT exist anywhere in the user_sessions table
        async with db.execute(
            "SELECT * FROM user_sessions WHERE session_token_hash = ?", (raw_token,)
        ) as cursor:
            assert await cursor.fetchone() is None

        # The computed hash MUST exist
        async with db.execute(
            "SELECT * FROM user_sessions WHERE session_token_hash = ?", (expected_hash,)
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row["user_id"] == principal_id
            assert row["revoked_at"] is None


async def test_session_me_active(client: httpx.AsyncClient) -> None:
    """GET /sessions/me returns public metadata for authenticated principal."""
    create_res = await client.post("/api/v1/sessions", json={})
    token = create_res.json()["session_token"]
    expected_user = create_res.json()["user_id"]

    response = await client.get(
        "/api/v1/sessions/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == expected_user
    assert data["is_active"] is True


async def test_session_me_unauthorized(client: httpx.AsyncClient) -> None:
    """Missing or invalid token returns 401."""
    # Missing header
    res_missing = await client.get("/api/v1/sessions/me")
    assert res_missing.status_code == 401

    # Malformed / unrecognized token
    res_invalid = await client.get(
        "/api/v1/sessions/me", headers={"Authorization": "Bearer not_a_valid_token"}
    )
    assert res_invalid.status_code == 401


async def test_session_revocation(client: httpx.AsyncClient) -> None:
    """DELETE /sessions/me revokes session, blocking future requests."""
    create_res = await client.post("/api/v1/sessions", json={})
    token = create_res.json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Verify active
    me_res = await client.get("/api/v1/sessions/me", headers=headers)
    assert me_res.status_code == 200

    # Revoke
    del_res = await client.delete("/api/v1/sessions/me", headers=headers)
    assert del_res.status_code == 204

    # Subsequent access rejected
    after_res = await client.get("/api/v1/sessions/me", headers=headers)
    assert after_res.status_code == 401
    assert "revoked" in after_res.json()["detail"].lower()


async def test_session_expiry(client: httpx.AsyncClient) -> None:
    """Expired session is rejected with 401."""
    create_res = await client.post("/api/v1/sessions", json={})
    token = create_res.json()["session_token"]
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    # Manually backdate expiration in SQLite
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE user_sessions SET expires_at = ? WHERE session_token_hash = ?",
            (past_iso, token_hash),
        )
        await db.commit()

    response = await client.get(
        "/api/v1/sessions/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()
