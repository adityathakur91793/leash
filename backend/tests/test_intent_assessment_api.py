"""Read-only, owner-scoped Intent Auditor assessment retrieval tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import aiosqlite
import httpx
import pytest

from app.core.config import settings
from app.core.security import proposal_content_hash, proposal_fingerprint, sign_payload
from app.db.database import get_db_path
from app.services.intent_auditor_client import IntentAuditorClient
from app.services.proposal_service import get_proposal
from app.services.mandate_service import get_mandate

pytestmark = pytest.mark.asyncio

ASSESSMENT_PATH = "/api/v1/proposals/{}/auditor-assessment"


def _mandate_payload() -> dict:
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


def _proposal_payload(mandate_id: str) -> dict:
    return {
        "mandate_id": mandate_id,
        "product_id": "sku-audit-1",
        "product_name": "Hardcover Notebook",
        "merchant": "amazon.com",
        "category": "books",
        "quantity": 1,
        "unit_price": "12.50",
        "currency": "USD",
        "tax": "1.00",
        "shipping": "2.00",
        "other_charges": "0.50",
        "product_source": "https://amazon.com/dp/sku-audit-1",
        "idempotency_key": "assessment-read-001",
    }


async def _create_owned_proposal(
    client: httpx.AsyncClient,
) -> tuple[str, str, dict[str, str]]:
    session = await client.post("/api/v1/sessions", json={})
    headers = {"Authorization": f"Bearer {session.json()['session_token']}"}
    mandate_response = await client.post(
        "/api/v1/mandates", json=_mandate_payload(), headers=headers
    )
    assert mandate_response.status_code == 201
    mandate_id = mandate_response.json()["mandate_id"]
    for action in ("confirm", "sign", "activate"):
        response = await client.post(
            f"/api/v1/mandates/{mandate_id}/{action}", headers=headers
        )
        assert response.status_code == 200
    proposal_response = await client.post(
        "/api/v1/proposals", json=_proposal_payload(mandate_id), headers=headers
    )
    assert proposal_response.status_code == 201
    return proposal_response.json()["proposal_id"], mandate_id, headers


async def _create_assessment(
    client: httpx.AsyncClient,
) -> tuple[str, str, dict[str, str]]:
    proposal_id, mandate_id, headers = await _create_owned_proposal(client)
    evaluated = await client.post(
        f"/api/v1/proposals/{proposal_id}/evaluate-review", headers=headers
    )
    assert evaluated.status_code == 200
    return proposal_id, mandate_id, headers


async def _assessment_rows(proposal_id: str) -> list[tuple]:
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT assessment_id, status, risk_level, finding_codes_json, "
            "affected_fields_json, created_at FROM intent_assessments "
            "WHERE proposal_id = ? ORDER BY created_at DESC, rowid DESC",
            (proposal_id,),
        )
        return await cursor.fetchall()


async def _insert_valid_copy(proposal_id: str, new_id: str) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT proposal_id, mandate_id, status, risk_level, finding_codes_json, "
            "affected_fields_json, proposal_content_hash, mandate_permissions_hash, "
            "policy_snapshot_hash, policy_version, schema_version, prompt_version, "
            "model_identifier, failure_code, created_at FROM intent_assessments "
            "WHERE proposal_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        await db.execute(
            "INSERT INTO intent_assessments (assessment_id, proposal_id, mandate_id, "
            "status, risk_level, finding_codes_json, affected_fields_json, "
            "proposal_content_hash, mandate_permissions_hash, policy_snapshot_hash, "
            "policy_version, schema_version, prompt_version, model_identifier, "
            "failure_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id, *row[:3], "HIGH", json.dumps(["BUDGET_TENSION"]),
             json.dumps(["mandate_budget"]), *row[6:]),
        )
        await db.commit()


async def test_assessment_get_requires_auth_and_returns_null_when_missing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_id, _, headers = await _create_owned_proposal(client)
    path = ASSESSMENT_PATH.format(proposal_id)

    monkeypatch.setattr(
        IntentAuditorClient, "assess",
        lambda *_args, **_kwargs: pytest.fail("GET must not invoke the auditor"),
    )
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"Authorization": "Bearer invalid-token"})).status_code == 401

    response = await client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"proposal_id": proposal_id, "assessment": None}
    assert await _assessment_rows(proposal_id) == []
    revoked = await client.delete("/api/v1/sessions/me", headers=headers)
    assert revoked.status_code == 204
    assert (await client.get(path, headers=headers)).status_code == 401


async def test_owner_get_returns_minimized_current_assessment_without_side_effects(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_id, _, headers = await _create_assessment(client)
    rows_before = await _assessment_rows(proposal_id)
    assert len(rows_before) == 1

    async def forbidden_auditor_call(*_args, **_kwargs):
        pytest.fail("assessment retrieval must never invoke the auditor")

    monkeypatch.setattr(IntentAuditorClient, "assess", forbidden_auditor_call)
    async with aiosqlite.connect(get_db_path()) as db:
        audit_before = (await (await db.execute("SELECT COUNT(*) FROM audit_logs")).fetchone())[0]
        proposal_status_before = (await (await db.execute(
            "SELECT status FROM proposals WHERE proposal_id = ?", (proposal_id,)
        )).fetchone())[0]
        orders_before = (await (await db.execute("SELECT COUNT(*) FROM paypal_orders")).fetchone())[0]
        tokens_before = (await (await db.execute(
            "SELECT COUNT(*) FROM approval_tokens WHERE proposal_id = ?", (proposal_id,)
        )).fetchone())[0]

    response = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["proposal_id"] == proposal_id
    assert set(body) == {"proposal_id", "assessment"}
    assessment = body["assessment"]
    assert set(assessment) == {
        "status", "risk_level", "finding_codes", "affected_fields", "created_at", "is_current",
    }
    assert assessment["status"] == "VALID"
    assert assessment["risk_level"] == "LOW"
    assert assessment["is_current"] is True

    async with aiosqlite.connect(get_db_path()) as db:
        audit_after = (await (await db.execute("SELECT COUNT(*) FROM audit_logs")).fetchone())[0]
        proposal_status_after = (await (await db.execute(
            "SELECT status FROM proposals WHERE proposal_id = ?", (proposal_id,)
        )).fetchone())[0]
        orders_after = (await (await db.execute("SELECT COUNT(*) FROM paypal_orders")).fetchone())[0]
        tokens_after = (await (await db.execute(
            "SELECT COUNT(*) FROM approval_tokens WHERE proposal_id = ?", (proposal_id,)
        )).fetchone())[0]
    assert await _assessment_rows(proposal_id) == rows_before
    assert audit_after == audit_before
    assert proposal_status_after == proposal_status_before == "EVALUATED"
    assert orders_after == orders_before == 0
    assert tokens_after == tokens_before == 0


async def test_foreign_and_missing_proposals_return_equivalent_404(
    client: httpx.AsyncClient,
) -> None:
    proposal_id, _, owner_headers = await _create_owned_proposal(client)
    session = await client.post("/api/v1/sessions", json={})
    foreign_headers = {"Authorization": f"Bearer {session.json()['session_token']}"}

    foreign = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=foreign_headers)
    missing = await client.get(
        ASSESSMENT_PATH.format("prp_does-not-exist"), headers=foreign_headers
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert (await client.get(ASSESSMENT_PATH.format(proposal_id), headers=owner_headers)).status_code == 200


async def test_proposal_integrity_failure_prevents_assessment_disclosure(
    client: httpx.AsyncClient,
) -> None:
    proposal_id, _, headers = await _create_assessment(client)
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE proposals SET total = '999.00' WHERE proposal_id = ?", (proposal_id,)
        )
        await db.commit()

    response = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=headers)
    assert response.status_code == 422
    assert "assessment" not in response.text.lower()


async def test_latest_assessment_uses_created_at_then_rowid_order(
    client: httpx.AsyncClient,
) -> None:
    proposal_id, _, headers = await _create_assessment(client)
    await _insert_valid_copy(proposal_id, "ia_latest_rowid")
    response = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=headers)
    assert response.status_code == 200
    assert response.json()["assessment"]["risk_level"] == "HIGH"
    assert response.json()["assessment"]["finding_codes"] == ["BUDGET_TENSION"]
    assert response.json()["assessment"]["is_current"] is True


async def test_valid_assessment_becomes_noncurrent_after_proposal_content_change(
    client: httpx.AsyncClient,
) -> None:
    proposal_id, _, headers = await _create_assessment(client)
    proposal = await get_proposal(proposal_id)
    changed = proposal.model_copy(update={
        "product_name": "Updated product title",
        "fingerprint": "",
        "content_hash": "",
    })
    changed.fingerprint = proposal_fingerprint(changed.model_dump())
    changed.content_hash = proposal_content_hash(changed.model_dump())
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE proposals SET product_name = ?, fingerprint = ?, content_hash = ? "
            "WHERE proposal_id = ?",
            (changed.product_name, changed.fingerprint, changed.content_hash, proposal_id),
        )
        await db.commit()

    response = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=headers)
    assert response.status_code == 200
    assert response.json()["assessment"]["status"] == "VALID"
    assert response.json()["assessment"]["is_current"] is False


async def test_valid_assessment_becomes_noncurrent_after_mandate_permissions_change(
    client: httpx.AsyncClient,
) -> None:
    proposal_id, mandate_id, headers = await _create_assessment(client)
    mandate = await get_mandate(mandate_id)
    changed = mandate.model_copy(update={"max_total_budget": Decimal("45.00")})
    changed.signature = sign_payload(changed.canonical_payload(), settings.MANDATE_SIGNING_KEY)
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE mandates SET max_total_budget = ?, signature = ? WHERE mandate_id = ?",
            (str(changed.max_total_budget), changed.signature, mandate_id),
        )
        await db.commit()

    response = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=headers)
    assert response.status_code == 200
    assert response.json()["assessment"]["status"] == "VALID"
    assert response.json()["assessment"]["is_current"] is False


@pytest.mark.parametrize("field,value", [
    ("policy_snapshot_hash", "0" * 64),
    ("policy_version", "P01-P12-old"),
    ("schema_version", "0.9"),
    ("prompt_version", "old-prompt"),
    ("model_identifier", "old-model"),
])
async def test_version_or_policy_snapshot_mismatch_is_not_current(
    client: httpx.AsyncClient, field: str, value: str,
) -> None:
    proposal_id, _, headers = await _create_assessment(client)
    allowed = {
        "policy_snapshot_hash", "policy_version", "schema_version", "prompt_version",
        "model_identifier",
    }
    assert field in allowed
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            f"UPDATE intent_assessments SET {field} = ? WHERE proposal_id = ?",
            (value, proposal_id),
        )
        await db.commit()

    response = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=headers)
    assert response.status_code == 200
    assert response.json()["assessment"]["is_current"] is False


@pytest.mark.parametrize("status,risk", [("STALE", "UNCERTAIN"), ("FAILED", "UNCERTAIN")])
async def test_persisted_stale_and_failed_assessments_remain_distinct(
    client: httpx.AsyncClient, status: str, risk: str,
) -> None:
    proposal_id, _, headers = await _create_assessment(client)
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE intent_assessments SET status = ?, risk_level = ?, "
            "failure_code = ? WHERE proposal_id = ?",
            (status, risk, "TEST_FAILURE" if status == "FAILED" else None, proposal_id),
        )
        await db.commit()

    response = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=headers)
    assert response.status_code == 200
    assert response.json()["assessment"]["status"] == status
    assert response.json()["assessment"]["risk_level"] == risk
    assert response.json()["assessment"]["is_current"] is False


@pytest.mark.parametrize("malformed_json", ["not-json", "null", '{"unexpected": true}'])
async def test_malformed_persisted_assessment_fails_safely_without_internal_data(
    client: httpx.AsyncClient, malformed_json: str,
) -> None:
    proposal_id, _, headers = await _create_assessment(client)
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE intent_assessments SET finding_codes_json = ? WHERE proposal_id = ?",
            (malformed_json, proposal_id),
        )
        await db.commit()

    response = await client.get(ASSESSMENT_PATH.format(proposal_id), headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "Assessment data is unavailable."}
    assert "hash" not in response.text.lower()
