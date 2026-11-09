"""Security and ownership tests for the public activity/audit projection."""

from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
import pytest
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.db.database import get_db_path
from app.schemas.audit import AuditActor, AuditEntityType, AuditEventType
from app.services.audit_service import AuditService

pytestmark = pytest.mark.asyncio


def _mandate_payload() -> dict[str, object]:
    return {
        "max_total_budget": "40.00", "currency": "USD",
        "allowed_merchants": ["amazon.com"], "allowed_categories": ["books"],
        "max_item_count": 2, "max_quantity_per_item": 1,
        "max_price_per_item": "25.00", "allow_subscriptions": False,
        "require_user_approval": True,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    }


def _proposal_payload(mandate_id: str, suffix: str) -> dict[str, object]:
    return {
        "mandate_id": mandate_id, "product_id": f"sku-{suffix}",
        "product_name": f"Hardcover Notebook {suffix}", "merchant": "amazon.com",
        "category": "books", "quantity": 1, "unit_price": "12.50",
        "currency": "USD", "tax": "0.00", "shipping": "0.00",
        "other_charges": "0.00", "product_source": "https://amazon.com/item",
        "idempotency_key": f"activity-test-{suffix}",
    }


async def _owner_with_proposal(client: httpx.AsyncClient, suffix: str) -> tuple[str, str]:
    created = await client.post("/api/v1/sessions", json={})
    token = created.json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    mandate = await client.post("/api/v1/mandates", json=_mandate_payload(), headers=headers)
    assert mandate.status_code == 201
    proposal = await client.post(
        "/api/v1/proposals", json=_proposal_payload(mandate.json()["mandate_id"], suffix),
        headers=headers,
    )
    assert proposal.status_code == 201
    return token, proposal.json()["proposal_id"]


async def test_activity_is_authenticated_owner_scoped_and_minimized(
    client: httpx.AsyncClient,
) -> None:
    owner_token, proposal_id = await _owner_with_proposal(client, "001")
    other_token, _ = await _owner_with_proposal(client, "002")
    await AuditService.append_event(
        event_type=AuditEventType.PROPOSAL_EVALUATED,
        entity_type=AuditEntityType.PROPOSAL,
        entity_id=proposal_id,
        actor=AuditActor.SYSTEM,
        payload={
            "content_hash": "h" * 64, "fingerprint": "f" * 64,
            "idempotency_key": "private-key-value",
            "product_source": "https://private.example/item",
            "evidence": {"nested": {"internal_id": "private-internal-id"}},
        },
    )

    assert (await client.get("/api/v1/activity/overview")).status_code == 401
    own = await client.get(
        "/api/v1/activity/overview", headers={"Authorization": f"Bearer {owner_token}"},
    )
    foreign = await client.get(
        "/api/v1/activity/overview", headers={"Authorization": f"Bearer {other_token}"},
    )
    assert own.status_code == foreign.status_code == 200
    own_data = own.json()
    foreign_data = foreign.json()
    assert own_data["total"] == 3
    assert {item["event_type"] for item in own_data["items"]} == {
        "MANDATE_CREATED", "PROPOSAL_CREATED", "PROPOSAL_EVALUATED",
    }
    assert foreign_data["total"] == 2
    assert "PROPOSAL_EVALUATED" not in {item["event_type"] for item in foreign_data["items"]}
    assert own_data["hash_chain_valid"] is True
    assert "does not prove" in own_data["integrity_note"]
    assert all("entity_id" not in item and "payload" not in item and "entry_hash" not in item
               for item in own_data["items"])
    assert all("sequence_number" not in item for item in own_data["items"])
    serialized = own.text
    assert "sequence_number" not in serialized
    for sensitive in (
        "content_hash", "fingerprint", "private-key-value", "private.example",
        "private-internal-id", "evidence", "prev_hash", "checkpoint_sig",
    ):
        assert sensitive not in serialized


async def test_completed_order_without_capture_id_is_not_a_confirmed_settlement(
    client: httpx.AsyncClient,
) -> None:
    token, proposal_id = await _owner_with_proposal(client, "003")
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT p.mandate_id, m.owner_user_id FROM proposals p
               JOIN mandates m ON m.mandate_id = p.mandate_id
               WHERE p.proposal_id = ?""", (proposal_id,),
        ) as cursor:
            row = await cursor.fetchone()
            mandate_id = row["mandate_id"]
            owner_id = row["owner_user_id"]
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO paypal_orders (
                order_id, proposal_id, mandate_id, owner_user_id, status, currency,
                total_amount, subtotal, tax, shipping, other_charges, approval_url,
                paypal_request_id, capture_id, capture_request_id, created_at,
                updated_at, captured_at
            ) VALUES (?, ?, ?, ?, 'COMPLETED', 'USD', '12.50', '12.50', '0.00',
                '0.00', '0.00', 'https://www.sandbox.paypal.com/checkoutnow?token=fake',
                'request-test-003', NULL, NULL, ?, ?, NULL)""",
            ("order-without-capture", proposal_id, mandate_id, owner_id, now, now),
        )
        await db.commit()

    response = await client.get(
        "/api/v1/activity/overview", headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["payment_status_counts"]["COMPLETED"] == 1
    assert data["confirmed_settlement_count"] == 0
    assert data["confirmed_settlement_totals"] == []


async def test_activity_counts_elapsed_mandates_as_expired_without_writing(
    client: httpx.AsyncClient,
) -> None:
    token, proposal_id = await _owner_with_proposal(client, "004")
    async with aiosqlite.connect(get_db_path()) as db:
        async with db.execute(
            "SELECT mandate_id FROM proposals WHERE proposal_id = ?", (proposal_id,),
        ) as cursor:
            mandate_id = (await cursor.fetchone())[0]
        await db.execute(
            "UPDATE mandates SET status = 'ACTIVE', expires_at = ? WHERE mandate_id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), mandate_id),
        )
        await db.commit()

    response = await client.get(
        "/api/v1/activity/overview", headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["mandate_status_counts"]["ACTIVE"] == 0
    assert response.json()["mandate_status_counts"]["EXPIRED"] == 1
    async with aiosqlite.connect(get_db_path()) as db:
        async with db.execute(
            "SELECT status FROM mandates WHERE mandate_id = ?", (mandate_id,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == "ACTIVE"


async def test_unresolved_attempt_summary_is_owner_scoped_and_separate_from_payments(
    client: httpx.AsyncClient,
) -> None:
    owner_token, owner_proposal = await _owner_with_proposal(client, "attempt-owner")
    other_token, other_proposal = await _owner_with_proposal(client, "attempt-other")
    async with aiosqlite.connect(get_db_path()) as db:
        async with db.execute(
            "SELECT mandate_id FROM proposals WHERE proposal_id = ?", (owner_proposal,),
        ) as cursor:
            mandate_id = (await cursor.fetchone())[0]
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    owner_proposals = [owner_proposal]
    for suffix in ("attempt-owner-2", "attempt-owner-3"):
        created = await client.post(
            "/api/v1/proposals", json=_proposal_payload(mandate_id, suffix),
            headers=owner_headers,
        )
        assert created.status_code == 201
        owner_proposals.append(created.json()["proposal_id"])
    async with aiosqlite.connect(get_db_path()) as db:
        records = []
        attempt_bindings = [
            (proposal_id, "owner-one", status)
            for proposal_id, status in zip(
                owner_proposals, ("UNKNOWN", "PROVIDER_CREATED", "RECONCILIATION_REQUIRED"),
                strict=True,
            )
        ] + [(other_proposal, "owner-two", "UNKNOWN")]
        for proposal_id, owner_id, status in attempt_bindings:
            async with db.execute(
                "SELECT m.owner_user_id FROM proposals p JOIN mandates m "
                "ON m.mandate_id = p.mandate_id WHERE p.proposal_id = ?",
                (proposal_id,),
            ) as cursor:
                actual_owner = (await cursor.fetchone())[0]
            records.append((
                f"attempt-{owner_id}-{status}", proposal_id, actual_owner,
                f"consent-{owner_id}-{status}", "binding-hash",
                "12.50", "USD", f"private-request-{owner_id}-{status}",
                "https://leash.example/return", "https://leash.example/cancel",
                status, f"private-provider-order-{owner_id}-{status}",
                "https://sandbox.paypal.com/private", "2026-11-09T00:00:00+00:00",
                "2026-11-09T00:00:00+00:00",
            ))
        await db.executemany(
            """INSERT INTO paypal_order_attempts (
                attempt_id, proposal_id, owner_user_id, consent_id, consent_binding_hash,
                expected_amount, expected_currency, provider_request_id, return_url,
                cancel_url, status, provider_order_id, approval_url, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            records,
        )
        await db.commit()

    owner_response = await client.get(
        "/api/v1/activity/overview", headers={"Authorization": f"Bearer {owner_token}"},
    )
    other_response = await client.get(
        "/api/v1/activity/overview", headers={"Authorization": f"Bearer {other_token}"},
    )
    assert owner_response.status_code == other_response.status_code == 200
    owner_data, other_data = owner_response.json(), other_response.json()
    assert owner_data["unresolved_checkout_attempt_count"] == 3
    assert owner_data["unresolved_checkout_attempt_status_counts"] == {
        "UNKNOWN": 1, "PROVIDER_CREATED": 1, "RECONCILIATION_REQUIRED": 1,
    }
    assert other_data["unresolved_checkout_attempt_count"] == 1
    assert other_data["unresolved_checkout_attempt_status_counts"] == {
        "UNKNOWN": 1, "PROVIDER_CREATED": 0, "RECONCILIATION_REQUIRED": 0,
    }
    assert sum(owner_data["payment_status_counts"].values()) == 0
    assert owner_data["confirmed_settlement_count"] == 0
    combined = owner_response.text + other_response.text
    assert "private-request-" not in combined
    assert "private-provider-order-" not in combined
    assert "sandbox.paypal.com" not in combined


async def test_activity_aggregates_share_read_transaction_before_chain_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.activity_service as activity_service

    statements: list[str] = []

    @asynccontextmanager
    async def tracked_connection():
        async with aiosqlite.connect(get_db_path()) as db:
            db.row_factory = aiosqlite.Row
            await db.set_trace_callback(statements.append)
            yield db

    async def verify_after_read():
        normalized = [statement.strip().upper() for statement in statements]
        assert normalized[0] == "BEGIN"
        assert normalized[-1] == "COMMIT"
        assert any("SELECT COUNT(*) FROM AUDIT_LOGS" in statement
                   for statement in normalized)
        return SimpleNamespace(valid=True)

    monkeypatch.setattr(activity_service, "get_connection", tracked_connection)
    monkeypatch.setattr(
        activity_service.AuditService, "verify_chain", staticmethod(verify_after_read),
    )
    result = await activity_service.get_activity_overview("owner-read-snapshot")
    assert result.hash_chain_valid is True
    assert [statement.strip().upper() for statement in statements].count("BEGIN") == 1
    assert [statement.strip().upper() for statement in statements].count("COMMIT") == 1
