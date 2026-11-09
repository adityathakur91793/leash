"""Purchase-consent API, transaction, and legacy fail-closed coverage."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiosqlite
import httpx
import pytest

from app.db.database import (
    get_connection,
    get_paypal_order_from_db,
    get_proposal_from_db,
    insert_paypal_order_in_db,
    list_audit_log_entries,
)
from app.schemas.audit import AuditActor, AuditEntityType, AuditEventType
from app.schemas.mandate import MandateCreate
from app.schemas.payment import PayPalOrderRecord, PayPalOrderStatus
from app.schemas.policy import DecisionOutcome
from app.schemas.proposal import ProposalCreate, ProposalStatus
from app.core.config import settings
from app.services.intent_auditor_client import IntentAuditorUnavailable
from app.services.approval_service import (
    MandateOwnershipError,
    consume_approval_token,
    issue_approval_token,
)
from app.services.audit_service import AuditService
from app.services.mandate_service import (
    MandateExpiredError,
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.policy_engine import generate_approval_token, hash_approval_token
from app.services.proposal_service import (
    ProposalTamperedError,
    create_proposal,
    evaluate_proposal_service,
)
from app.services.payment_service import (
    capture_payment_order, create_payment_order, recover_proposal_checkout,
    verify_payment_return,
)
from app.services.paypal_service import PayPalNetworkError, PayPalOrderError
from app.services.purchase_consent_service import (
    PurchaseConsentDeniedError,
    PurchaseConsentRequiredError,
    record_purchase_consent,
    require_current_purchase_consent,
)


async def _auth(client: httpx.AsyncClient) -> tuple[dict[str, str], str]:
    response = await client.post("/api/v1/sessions", json={})
    body = response.json()
    return {"Authorization": f"Bearer {body['session_token']}"}, body["user_id"]


async def _proposal(owner: str, *, allow: bool, hard_block: bool = False) -> str:
    mandate = await create_draft_mandate(
        MandateCreate(
            max_total_budget=Decimal("5.00" if hard_block else "500.00"),
            currency="USD",
            allowed_merchants=["shop.example"],
            allowed_categories=["books"],
            max_item_count=3,
            max_quantity_per_item=1,
            max_price_per_item=Decimal(
                "5.00" if hard_block or not allow else "20.00"
            ),
            allow_subscriptions=False,
            require_user_approval=not allow,
            expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        ),
        owner_user_id=owner,
    )
    await confirm_mandate(mandate.mandate_id)
    await sign_mandate(mandate.mandate_id)
    await activate_mandate(mandate.mandate_id)
    proposal, _ = await create_proposal(
        ProposalCreate(
            mandate_id=mandate.mandate_id,
            product_id=f"book-{owner}-{allow}",
            product_name="Secure purchase test book",
            merchant="shop.example",
            category="books",
            quantity=1,
            unit_price=Decimal("10.00"),
            currency="USD",
            product_source="https://shop.example/book",
        )
    )
    decision = await evaluate_proposal_service(proposal.proposal_id)
    expected = (DecisionOutcome.BLOCK if hard_block else
                DecisionOutcome.ALLOW if allow else DecisionOutcome.REQUIRE_APPROVAL)
    assert decision.decision == expected
    return proposal.proposal_id


async def _complete_review(proposal_id: str, owner: str) -> str:
    issued = await issue_approval_token(proposal_id, owner)
    success, reason = await consume_approval_token(
        proposal_id=proposal_id,
        token=issued["token"],
        principal_id=owner,
        issued_at_iso=issued["issued_at"],
        nonce=issued["nonce"],
    )
    assert success, reason
    return issued["token"]


async def _change_assessment(proposal_id: str, **values: str) -> None:
    """Mutate a persisted assessment to model a newer authoritative result."""
    allowed = {
        "assessment_id", "status", "risk_level", "finding_codes_json",
        "affected_fields_json", "policy_version", "schema_version",
        "prompt_version", "model_identifier", "created_at",
    }
    if not values or not set(values) <= allowed:
        raise AssertionError("Assessment test mutation contains unsupported fields.")
    assignments = ", ".join(f"{field} = ?" for field in values)
    async with get_connection() as db:
        await db.execute(
            f"UPDATE intent_assessments SET {assignments} WHERE proposal_id = ?",
            (*values.values(), proposal_id),
        )
        await db.commit()


async def _assert_rejected_token_attempt_is_audited(
    proposal_id: str,
    owner: str,
    *,
    expected_status: ProposalStatus,
    expected_reason: str,
    token_before: tuple,
) -> None:
    """Assert a rejected token race rolls back authorization and emits one event."""
    proposal = await get_proposal_from_db(proposal_id)
    assert proposal is not None and proposal.status == expected_status
    async with get_connection() as db:
        token_after = await (await db.execute(
            "SELECT * FROM approval_tokens WHERE proposal_id = ?", (proposal_id,),
        )).fetchone()
        assert tuple(token_after) == token_before
        assert token_after[4] is None  # consumed_at

        consent_count = await (await db.execute(
            "SELECT COUNT(*) FROM purchase_consents WHERE proposal_id = ?",
            (proposal_id,),
        )).fetchone()
        assert consent_count[0] == 0

        review_events = await (await db.execute(
            "SELECT payload_json FROM audit_logs WHERE entity_id = ? "
            "AND event_type = 'EXCEPTION_APPROVED' ORDER BY sequence_number",
            (proposal_id,),
        )).fetchall()
        assert len(review_events) == 1
        assert json.loads(review_events[0][0])["action"] == "token_issued"
        consent_events = await (await db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE entity_id = ? "
            "AND event_type = 'PURCHASE_CONSENT_RECORDED'", (proposal_id,),
        )).fetchone()
        assert consent_events[0] == 0

        failure_events = await (await db.execute(
            "SELECT payload_json FROM audit_logs WHERE entity_id = ? "
            "AND event_type = 'REPLAY_ATTEMPT_BLOCKED' ORDER BY sequence_number",
            (proposal_id,),
        )).fetchall()
        assert len(failure_events) == 1
        assert json.loads(failure_events[0][0]) == {
            "action": "token_consume_failed",
            "principal_id": owner,
            "proposal_id": proposal_id,
            "reason": expected_reason,
        }


@pytest.mark.asyncio
async def test_assessment_binding_race_rolls_back_and_audits_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.intent_audit_service as intent_audit_service

    owner = "assessment-race-audit-owner"
    proposal_id = await _proposal(owner, allow=False)
    issued = await issue_approval_token(proposal_id, owner)
    async with get_connection() as db:
        token_before = tuple(await (await db.execute(
            "SELECT * FROM approval_tokens WHERE proposal_id = ?", (proposal_id,),
        )).fetchone())

    original_require_assessment = intent_audit_service.require_current_assessment
    calls = 0

    async def changed_on_transaction_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await original_require_assessment(*args, **kwargs)
        if kwargs.get("db") is not None:
            return {**result, "assessment_binding": "f" * 64}
        return result

    monkeypatch.setattr(
        intent_audit_service,
        "require_current_assessment",
        changed_on_transaction_read,
    )
    success, reason = await consume_approval_token(
        proposal_id, issued["token"], owner, issued["issued_at"], issued["nonce"]
    )

    assert calls == 2
    assert success is False
    assert reason == "Intent assessment changed before token consumption."
    await _assert_rejected_token_attempt_is_audited(
        proposal_id,
        owner,
        expected_status=ProposalStatus.EVALUATED,
        expected_reason=reason,
        token_before=token_before,
    )


@pytest.mark.asyncio
async def test_proposal_status_race_rolls_back_and_audits_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.approval_service as approval_service

    owner = "proposal-status-race-audit-owner"
    proposal_id = await _proposal(owner, allow=False)
    issued = await issue_approval_token(proposal_id, owner)
    async with get_connection() as db:
        token_before = tuple(await (await db.execute(
            "SELECT * FROM approval_tokens WHERE proposal_id = ?", (proposal_id,),
        )).fetchone())

    original_policy_decision = approval_service.current_policy_decision
    calls = 0

    async def status_changes_after_preflight(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await original_policy_decision(*args, **kwargs)
        if calls == 1 and kwargs.get("db") is None:
            async with get_connection() as db:
                await db.execute(
                    "UPDATE proposals SET status = 'REJECTED' WHERE proposal_id = ?",
                    (proposal_id,),
                )
                await db.commit()
        return result

    monkeypatch.setattr(
        approval_service,
        "current_policy_decision",
        status_changes_after_preflight,
    )
    success, reason = await consume_approval_token(
        proposal_id, issued["token"], owner, issued["issued_at"], issued["nonce"]
    )

    assert calls == 2
    assert success is False
    assert reason == "Proposal is no longer awaiting user review."
    await _assert_rejected_token_attempt_is_audited(
        proposal_id,
        owner,
        expected_status=ProposalStatus.REJECTED,
        expected_reason=reason,
        token_before=token_before,
    )


@pytest.mark.asyncio
async def test_allow_uses_consent_without_exception_token(client: httpx.AsyncClient) -> None:
    headers, owner = await _auth(client)
    proposal_id = await _proposal(owner, allow=True)

    token_response = await client.post(
        f"/api/v1/proposals/{proposal_id}/approval-token",
        json={"proposal_id": proposal_id},
        headers=headers,
    )
    assert token_response.status_code == 400

    response = await client.post(
        f"/api/v1/proposals/{proposal_id}/purchase-consent",
        json={"confirm_purchase": True},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["policy_decision"] == "ALLOW"
    assert response.json()["status"] == "APPROVED"
    proposal = await get_proposal_from_db(proposal_id)
    assert proposal is not None and proposal.status == ProposalStatus.APPROVED
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT review_token_hash, assessment_binding FROM purchase_consents "
            "WHERE proposal_id = ?",
            (proposal_id,),
        )
        consent = await cursor.fetchone()
        assert consent[0] is None
        assert isinstance(consent[1], str) and len(consent[1]) == 64


@pytest.mark.asyncio
async def test_require_approval_requires_review_then_separate_consent(
    client: httpx.AsyncClient,
) -> None:
    headers, owner = await _auth(client)
    proposal_id = await _proposal(owner, allow=False)
    path = f"/api/v1/proposals/{proposal_id}/purchase-consent"
    payload = {"confirm_purchase": True}

    before_review = await client.post(path, json=payload, headers=headers)
    assert before_review.status_code == 409
    assert "exception-review" in before_review.json()["detail"]
    assert (await get_proposal_from_db(proposal_id)).status == ProposalStatus.EVALUATED

    token = await _complete_review(proposal_id, owner)
    reviewed = await get_proposal_from_db(proposal_id)
    assert reviewed is not None and reviewed.status == ProposalStatus.EVALUATED
    after_review = await client.post(path, json=payload, headers=headers)
    assert after_review.status_code == 200
    assert after_review.json()["policy_decision"] == "REQUIRE_APPROVAL"
    assert (await get_proposal_from_db(proposal_id)).status == ProposalStatus.APPROVED

    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT token_hash, owner_user_id, content_hash, assessment_binding, consumed_at "
            "FROM approval_tokens WHERE proposal_id = ?",
            (proposal_id,),
        )
        review = await cursor.fetchone()
        assert review[0] == hash_approval_token(token)
        assert review[0] != token
        assert review[1] == owner
        assert review[2] == reviewed.content_hash
        assert isinstance(review[3], str) and len(review[3]) == 64
        assert review[4] is not None
        consent_binding = await (await db.execute(
            "SELECT assessment_binding FROM purchase_consents WHERE proposal_id = ?",
            (proposal_id,),
        )).fetchone()
        assert consent_binding[0] == review[3]


@pytest.mark.asyncio
async def test_legacy_token_binding_is_verified_and_persisted_on_consumption() -> None:
    owner = "legacy-review-token-owner"
    proposal_id = await _proposal(owner, allow=False)
    issued = await issue_approval_token(proposal_id, owner)
    # Simulate a token record created before migration 9 added these bindings.
    async with get_connection() as db:
        await db.execute(
            "UPDATE approval_tokens SET owner_user_id = NULL, content_hash = NULL, "
            "assessment_binding = NULL "
            "WHERE proposal_id = ?",
            (proposal_id,),
        )
        await db.commit()

    success, reason = await consume_approval_token(
        proposal_id=proposal_id,
        token=issued["token"],
        principal_id=owner,
        issued_at_iso=issued["issued_at"],
        nonce=issued["nonce"],
    )
    assert success, reason
    await record_purchase_consent(proposal_id, owner)
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT owner_user_id, content_hash, assessment_binding, consumed_at "
            "FROM approval_tokens "
            "WHERE proposal_id = ?",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == owner
        assert row[1] == (await get_proposal_from_db(proposal_id)).content_hash
        assert row[2] is not None and len(row[2]) == 64
        assert row[3] is not None


@pytest.mark.asyncio
async def test_unbound_legacy_hmac_token_is_rejected_without_guessing_assessment() -> None:
    owner = "unbound-legacy-token-owner"
    proposal_id = await _proposal(owner, allow=False)
    proposal = await get_proposal_from_db(proposal_id)
    assert proposal is not None
    issued_at = datetime.now(timezone.utc)
    nonce = "legacy-unbound-nonce"
    token = generate_approval_token(
        proposal_id=proposal_id,
        content_hash=proposal.content_hash,
        user_session_id=owner,
        approval_timestamp=issued_at,
        nonce=nonce,
    )
    token_hash = hash_approval_token(token)
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO approval_tokens "
            "(token_hash, proposal_id, owner_user_id, content_hash, assessment_binding, "
            "expires_at, created_at, consumed_at) VALUES (?, ?, NULL, NULL, NULL, ?, ?, NULL)",
            (
                token_hash, proposal_id,
                (issued_at + timedelta(minutes=15)).isoformat(), issued_at.isoformat(),
            ),
        )
        await db.commit()

    success, _ = await consume_approval_token(
        proposal_id, token, owner, issued_at.isoformat(), nonce
    )
    assert success is False
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT owner_user_id, content_hash, assessment_binding, consumed_at "
            "FROM approval_tokens WHERE token_hash = ?", (token_hash,),
        )).fetchone()
        assert tuple(row) == (None, None, None, None)


@pytest.mark.asyncio
async def test_token_issued_for_assessment_a_is_rejected_for_assessment_b() -> None:
    owner = "assessment-token-owner"
    proposal_id = await _proposal(owner, allow=False)
    issued = await issue_approval_token(proposal_id, owner)
    await _change_assessment(
        proposal_id,
        assessment_id="ia_replacement_assessment",
        risk_level="HIGH",
        finding_codes_json=json.dumps(["BUDGET_TENSION"]),
        affected_fields_json=json.dumps(["total"]),
    )

    success, reason = await consume_approval_token(
        proposal_id, issued["token"], owner, issued["issued_at"], issued["nonce"]
    )
    assert success is False
    assert "verification failed" in reason.lower()
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT consumed_at, assessment_binding FROM approval_tokens "
            "WHERE proposal_id = ?", (proposal_id,),
        )).fetchone()
        assert row[0] is None
        assert len(row[1]) == 64
        audit_rows = await (await db.execute(
            "SELECT payload_json FROM audit_logs WHERE entity_id = ? "
            "AND event_type = 'EXCEPTION_APPROVED' ORDER BY sequence_number",
            (proposal_id,),
        )).fetchall()
        assert len(audit_rows) == 1  # issuance only; no consumption event
        assert json.loads(audit_rows[0][0])["action"] == "token_issued"
        consent_rows = await (await db.execute(
            "SELECT COUNT(*) FROM purchase_consents WHERE proposal_id = ?",
            (proposal_id,),
        )).fetchone()
        assert consent_rows[0] == 0
        purchase_events = await (await db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE entity_id = ? "
            "AND event_type = 'PURCHASE_CONSENT_RECORDED'", (proposal_id,),
        )).fetchone()
        assert purchase_events[0] == 0
        replay_events = await (await db.execute(
            "SELECT payload_json FROM audit_logs WHERE entity_id = ? "
            "AND event_type = 'REPLAY_ATTEMPT_BLOCKED' ORDER BY sequence_number",
            (proposal_id,),
        )).fetchall()
        assert len(replay_events) == 1
        replay_payload = json.loads(replay_events[0][0])
        assert replay_payload == {
            "action": "token_consume_failed",
            "principal_id": owner,
            "proposal_id": proposal_id,
            "reason": reason,
        }
    proposal = await get_proposal_from_db(proposal_id)
    assert proposal is not None and proposal.status == ProposalStatus.EVALUATED


@pytest.mark.asyncio
async def test_review_for_assessment_a_cannot_satisfy_consent_for_b() -> None:
    owner = "assessment-consent-review-owner"
    proposal_id = await _proposal(owner, allow=False)
    await _complete_review(proposal_id, owner)
    await _change_assessment(
        proposal_id,
        risk_level="HIGH",
        finding_codes_json=json.dumps(["AMBIGUOUS_INTENT"]),
    )

    with pytest.raises(PurchaseConsentDeniedError, match="exception-review"):
        await record_purchase_consent(proposal_id, owner)
    async with get_connection() as db:
        count = await (await db.execute(
            "SELECT COUNT(*) FROM purchase_consents WHERE proposal_id = ?",
            (proposal_id,),
        )).fetchone()
        assert count[0] == 0
        audit_count = await (await db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE entity_id = ? "
            "AND event_type = 'PURCHASE_CONSENT_RECORDED'", (proposal_id,),
        )).fetchone()
        assert audit_count[0] == 0
        assert (await get_proposal_from_db(proposal_id, db=db)).status == ProposalStatus.EVALUATED


@pytest.mark.asyncio
async def test_assessment_change_invalidates_consent_for_order_and_capture() -> None:
    class PayPal:
        create_calls = 0
        get_calls = 0

        async def create_order(self, **kwargs):
            self.create_calls += 1
            return {
                "order_id": "assessment-bound-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=opaque",
                "status": "CREATED",
            }

        async def get_order(self, order_id):
            self.get_calls += 1
            return {"id": order_id, "status": "APPROVED", "purchase_units": []}

    owner = "assessment-order-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)
    paypal = PayPal()
    await create_payment_order(proposal_id, owner, paypal_client=paypal)
    assert paypal.create_calls == 1

    await _change_assessment(
        proposal_id,
        finding_codes_json=json.dumps(["PRICE_INCONSISTENCY"]),
        affected_fields_json=json.dumps(["unit_price"]),
    )
    with pytest.raises(PurchaseConsentRequiredError):
        await create_payment_order(proposal_id, owner, paypal_client=paypal)
    assert paypal.create_calls == 1

    with pytest.raises(PurchaseConsentRequiredError):
        await capture_payment_order(
            "assessment-bound-order", owner, paypal_client=paypal
        )
    assert paypal.get_calls == 0


@pytest.mark.asyncio
async def test_ambiguous_order_creation_reuses_durable_provider_idempotency_key() -> None:
    owner = "ambiguous-order-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        request_ids = []
        calls = 0

        async def create_order(self, **kwargs):
            self.calls += 1
            self.request_ids.append(kwargs["paypal_request_id"])
            if self.calls == 1:
                raise PayPalNetworkError("simulated provider timeout")
            return {
                "order_id": "recovered-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=recovered-order",
                "status": "CREATED",
            }

    paypal = PayPal()
    with pytest.raises(PayPalNetworkError):
        await create_payment_order(proposal_id, owner, paypal_client=paypal)
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT attempt_id, status, provider_request_id FROM paypal_order_attempts "
            "WHERE proposal_id = ?", (proposal_id,),
        )).fetchone()
        assert row[1] == "UNKNOWN"
    response = await create_payment_order(proposal_id, owner, paypal_client=paypal)
    assert response.order_id == "recovered-order"
    assert paypal.calls == 2
    assert paypal.request_ids[0] == paypal.request_ids[1] == row[2]
    async with get_connection() as db:
        attempts = await (await db.execute(
            "SELECT COUNT(*) FROM paypal_order_attempts WHERE proposal_id = ?", (proposal_id,),
        )).fetchone()
        stored = await (await db.execute(
            "SELECT status, provider_order_id FROM paypal_order_attempts WHERE proposal_id = ?",
            (proposal_id,),
        )).fetchone()
        assert attempts[0] == 1
        assert stored == ("PROVIDER_CREATED", "recovered-order")


@pytest.mark.asyncio
async def test_concurrent_order_request_does_not_dispatch_provider_twice() -> None:
    owner = "concurrent-order-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    class PayPal:
        calls = 0

        async def create_order(self, **kwargs):
            self.calls += 1
            provider_started.set()
            await release_provider.wait()
            return {
                "order_id": "single-dispatched-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=opaque",
                "status": "CREATED",
            }

    paypal = PayPal()
    first = asyncio.create_task(create_payment_order(proposal_id, owner, paypal_client=paypal))
    await provider_started.wait()
    with pytest.raises(PayPalOrderError, match="already in progress"):
        await create_payment_order(proposal_id, owner, paypal_client=paypal)
    release_provider.set()
    result = await first
    assert result.order_id == "single-dispatched-order"
    assert paypal.calls == 1


@pytest.mark.asyncio
async def test_concurrent_recovery_of_anchored_order_never_creates_another_order() -> None:
    owner = "anchored-order-recovery-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class LostResponse:
        async def create_order(self, **kwargs):
            raise PayPalNetworkError("response lost after provider creation")

    with pytest.raises(PayPalNetworkError):
        await create_payment_order(proposal_id, owner, paypal_client=LostResponse())
    async with get_connection() as db:
        await db.execute(
            "UPDATE paypal_order_attempts SET provider_order_id=?, approval_url=? "
            "WHERE proposal_id=?",
            ("already-created-provider-order",
             "https://www.sandbox.paypal.com/checkoutnow?token=opaque", proposal_id),
        )
        await db.commit()

    both_lookups_started = asyncio.Event()
    release_lookups = asyncio.Event()

    class RecoveryPayPal:
        get_calls = 0
        create_calls = 0

        async def get_order(self, order_id):
            self.get_calls += 1
            if self.get_calls == 2:
                both_lookups_started.set()
            await release_lookups.wait()
            return {
                "id": order_id, "status": "APPROVED", "purchase_units": [{
                    "reference_id": proposal_id,
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }],
            }

        async def create_order(self, **kwargs):
            self.create_calls += 1
            raise AssertionError("Recovery must look up the persisted provider order ID.")

    recovery = RecoveryPayPal()
    first = asyncio.create_task(create_payment_order(
        proposal_id, owner, paypal_client=recovery,
    ))
    second = asyncio.create_task(create_payment_order(
        proposal_id, owner, paypal_client=recovery,
    ))
    await both_lookups_started.wait()
    release_lookups.set()
    responses = await asyncio.gather(first, second)
    assert [response.order_id for response in responses] == [
        "already-created-provider-order", "already-created-provider-order",
    ]
    assert recovery.create_calls == 0
    async with get_connection() as db:
        rows = await (await db.execute(
            "SELECT order_id FROM paypal_orders WHERE proposal_id=?", (proposal_id,),
        )).fetchall()
    assert rows == [("already-created-provider-order",)]


@pytest.mark.asyncio
async def test_ambiguous_order_attempt_refuses_replay_after_provider_key_window() -> None:
    owner = "expired-order-key-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        calls = 0

        async def create_order(self, **kwargs):
            self.calls += 1
            raise PayPalNetworkError("simulated provider timeout")

    paypal = PayPal()
    with pytest.raises(PayPalNetworkError):
        await create_payment_order(proposal_id, owner, paypal_client=paypal)
    async with get_connection() as db:
        await db.execute(
            "UPDATE paypal_order_attempts SET created_at = ? WHERE proposal_id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(), proposal_id),
        )
        await db.commit()
    with pytest.raises(PayPalOrderError, match="idempotency recovery window expired"):
        await create_payment_order(proposal_id, owner, paypal_client=paypal)
    assert paypal.calls == 1
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT status FROM paypal_order_attempts WHERE proposal_id = ?", (proposal_id,),
        )).fetchone()
        assert row == ("RECONCILIATION_REQUIRED",)


@pytest.mark.asyncio
async def test_verified_completed_provider_order_recovers_uncertain_capture() -> None:
    owner = "verified-capture-recovery-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        async def create_order(self, **kwargs):
            return {
                "order_id": "completed-after-timeout-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=opaque",
                "status": "CREATED",
            }

        async def get_order(self, order_id):
            assert order_id == "completed-after-timeout-order"
            return {
                "id": order_id,
                "status": "COMPLETED",
                "purchase_units": [{
                    "reference_id": proposal_id,
                    "amount": {"currency_code": "USD", "value": "10.00"},
                    "payments": {"captures": [{
                        "id": "provider-confirmed-capture",
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "10.00"},
                    }]},
                }],
            }

        async def capture_order(self, *args, **kwargs):
            raise AssertionError("A completed provider capture must not be dispatched again.")

    paypal = PayPal()
    await create_payment_order(proposal_id, owner, paypal_client=paypal)
    async with get_connection() as db:
        await db.execute(
            "UPDATE paypal_orders SET status='RECONCILIATION_REQUIRED' "
            "WHERE order_id='completed-after-timeout-order'"
        )
        await db.commit()

    result = await verify_payment_return(
        proposal_id, "completed-after-timeout-order", owner, paypal_client=paypal
    )
    assert result.payment_completed is True
    assert result.status == PayPalOrderStatus.COMPLETED
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT status, capture_id FROM paypal_orders WHERE order_id = ?",
            ("completed-after-timeout-order",),
        )).fetchone()
        proposal = await get_proposal_from_db(proposal_id, db=db)
    assert row == ("COMPLETED", "provider-confirmed-capture")
    assert proposal is not None and proposal.status == ProposalStatus.SETTLED


@pytest.mark.asyncio
async def test_completed_status_without_capture_id_is_not_reported_as_success() -> None:
    from app.services.payment_service import get_payment_checkout_state

    owner = "missing-capture-id-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        async def create_order(self, **kwargs):
            return {
                "order_id": "missing-capture-id-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=opaque",
                "status": "CREATED",
            }

    await create_payment_order(proposal_id, owner, paypal_client=PayPal())
    async with get_connection() as db:
        await db.execute(
            "UPDATE paypal_orders SET status='COMPLETED', capture_id=NULL "
            "WHERE order_id=?", ("missing-capture-id-order",),
        )
        await db.execute(
            "UPDATE mandates SET expires_at=? WHERE owner_user_id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), owner),
        )
        await db.commit()
    state = await get_payment_checkout_state(proposal_id, owner)
    assert state.status == PayPalOrderStatus.RECONCILIATION_REQUIRED
    assert state.payment_status == PayPalOrderStatus.RECONCILIATION_REQUIRED
    assert state.consent_recorded is False
    assert state.order_id is None
    assert state.approval_url is None
    assert state.recovery_available is True


@pytest.mark.asyncio
async def test_expired_authorization_blocks_new_capture_but_records_verified_completion() -> None:
    owner = "expired-auth-capture-recovery-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        get_calls = 0
        capture_calls = 0
        create_calls = 0

        async def create_order(self, **kwargs):
            self.create_calls += 1
            return {
                "order_id": "expired-auth-recovery-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=opaque",
                "status": "CREATED",
            }

        async def get_order(self, order_id):
            self.get_calls += 1
            return {
                "id": order_id, "status": "COMPLETED", "purchase_units": [{
                    "reference_id": proposal_id,
                    "amount": {"currency_code": "USD", "value": "10.00"},
                    "payments": {"captures": [{
                        "id": "verified-after-expiry-capture", "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "10.00"},
                    }]},
                }],
            }

        async def capture_order(self, *args, **kwargs):
            self.capture_calls += 1
            raise AssertionError("Reconciliation must not dispatch a new capture.")

    paypal = PayPal()
    await create_payment_order(proposal_id, owner, paypal_client=paypal)
    proposal = await get_proposal_from_db(proposal_id)
    assert proposal is not None
    async with get_connection() as db:
        await db.execute(
            "UPDATE mandates SET expires_at=? WHERE mandate_id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
             proposal.mandate_id),
        )
        await db.commit()

    with pytest.raises(MandateExpiredError, match="expired"):
        await capture_payment_order(
            "expired-auth-recovery-order", owner, paypal_client=paypal,
        )
    assert paypal.get_calls == 0
    assert paypal.capture_calls == 0

    result = await verify_payment_return(
        proposal_id, "expired-auth-recovery-order", owner, paypal_client=paypal,
    )
    assert result.payment_completed is True
    assert paypal.get_calls == 1
    assert paypal.capture_calls == 0
    async with get_connection() as db:
        order = await (await db.execute(
            "SELECT status, capture_id FROM paypal_orders WHERE order_id=?",
            ("expired-auth-recovery-order",),
        )).fetchone()
        settled = await get_proposal_from_db(proposal_id, db=db)
    assert order == ("COMPLETED", "verified-after-expiry-capture")
    assert settled is not None and settled.status == ProposalStatus.SETTLED
    async with get_connection() as db:
        await db.execute(
            "DELETE FROM paypal_orders WHERE order_id=?",
            ("expired-auth-recovery-order",),
        )
        await db.commit()
    recovered = await create_payment_order(
        proposal_id, owner, paypal_client=paypal,
    )
    assert recovered.status == PayPalOrderStatus.COMPLETED
    assert paypal.get_calls == 2
    assert paypal.capture_calls == 0
    assert paypal.create_calls == 1
    async with get_connection() as db:
        recovered_order = await (await db.execute(
            "SELECT status, capture_id FROM paypal_orders WHERE order_id=?",
            ("expired-auth-recovery-order",),
        )).fetchone()
    assert recovered_order == ("COMPLETED", "verified-after-expiry-capture")


@pytest.mark.asyncio
async def test_login_after_expired_return_resumes_owner_scoped_factual_recovery(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload/login uses proposal scope and never needs browser-supplied order IDs."""
    from app.api.v1 import payments as payments_api

    old_headers, owner = await _auth(client)
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        capture_calls = 0

        async def create_order(self, **kwargs):
            return {
                "order_id": "reload-recovery-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=opaque",
                "status": "CREATED",
            }

        async def get_order(self, order_id):
            assert order_id == "reload-recovery-order"
            return {
                "id": order_id, "status": "COMPLETED", "purchase_units": [{
                    "reference_id": proposal_id,
                    "amount": {"currency_code": "USD", "value": "10.00"},
                    "payments": {"captures": [{
                        "id": "reload-real-capture", "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "10.00"},
                    }]},
                }],
            }

        async def capture_order(self, *args, **kwargs):
            self.capture_calls += 1
            raise AssertionError("Factual recovery must never dispatch capture.")

    paypal = PayPal()
    async def recover_for_test(proposal_id: str, principal_id: str):
        return await recover_proposal_checkout(
            proposal_id, principal_id, paypal_client=paypal,
        )

    monkeypatch.setattr(payments_api, "recover_proposal_checkout", recover_for_test)
    await create_payment_order(proposal_id, owner, paypal_client=paypal)
    proposal = await get_proposal_from_db(proposal_id)
    assert proposal is not None
    async with get_connection() as db:
        await db.execute(
            "UPDATE mandates SET expires_at=? WHERE mandate_id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
             proposal.mandate_id),
        )
        await db.commit()

    # Renew a session for the same server-derived principal, then expire the
    # original return session to model login after browser reload.
    renewed = await client.post("/api/v1/sessions", json={}, headers=old_headers)
    assert renewed.status_code == 201
    renewed_headers = {"Authorization": f"Bearer {renewed.json()['session_token']}"}
    async with get_connection() as db:
        await db.execute(
            "UPDATE user_sessions SET expires_at=? WHERE session_token_hash=("
            "SELECT session_token_hash FROM user_sessions WHERE user_id=? "
            "ORDER BY created_at LIMIT 1)",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), owner),
        )
        await db.commit()

    state = await client.get(
        f"/api/v1/proposals/{proposal_id}/checkout", headers=renewed_headers,
    )
    assert state.status_code == 200
    assert state.json()["recovery_available"] is True
    assert state.json()["order_id"] is None
    assert state.json()["approval_url"] is None
    recovered = await client.post(
        f"/api/v1/proposals/{proposal_id}/checkout/reconcile",
        headers=renewed_headers,
    )
    assert recovered.status_code == 200
    assert recovered.json() == {
        "proposal_id": proposal_id, "status": "COMPLETED",
        "provider_status": "COMPLETED", "payment_completed": True,
    }
    assert "reload-recovery-order" not in recovered.text
    assert paypal.capture_calls == 0
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT status, capture_id FROM paypal_orders WHERE order_id=?",
            ("reload-recovery-order",),
        )).fetchone()
    assert row == ("COMPLETED", "reload-real-capture")


@pytest.mark.asyncio
async def test_recovery_of_existing_order_does_not_claim_noncompleted_payment() -> None:
    proposal_id = await _proposal("not-completed-recovery-owner", allow=True)
    owner = "not-completed-recovery-owner"
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        capture_calls = 0

        async def create_order(self, **kwargs):
            return {"order_id": "not-completed-order",
                    "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
                    "status": "CREATED"}

        async def get_order(self, order_id):
            return {"id": order_id, "status": "APPROVED", "purchase_units": [{
                "reference_id": proposal_id,
                "amount": {"currency_code": "USD", "value": "10.00"},
            }]}

        async def capture_order(self, *args, **kwargs):
            self.capture_calls += 1
            raise AssertionError("Recovery may not capture.")

    paypal = PayPal()
    await create_payment_order(proposal_id, owner, paypal_client=paypal)
    result = await recover_proposal_checkout(proposal_id, owner, paypal_client=paypal)
    assert result.payment_completed is False
    assert result.provider_status == "APPROVED"
    assert result.status == PayPalOrderStatus.CREATED
    assert paypal.capture_calls == 0


@pytest.mark.asyncio
async def test_recovery_loads_provider_order_id_from_attempt_when_local_order_is_missing() -> None:
    proposal_id = await _proposal("attempt-only-recovery-owner", allow=True)
    owner = "attempt-only-recovery-owner"
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        create_calls = 0
        capture_calls = 0

        async def create_order(self, **kwargs):
            self.create_calls += 1
            return {"order_id": "attempt-only-provider-order",
                    "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
                    "status": "CREATED"}

        async def get_order(self, order_id):
            assert order_id == "attempt-only-provider-order"
            return {"id": order_id, "status": "COMPLETED", "purchase_units": [{
                "reference_id": proposal_id,
                "amount": {"currency_code": "USD", "value": "10.00"},
                "payments": {"captures": [{
                    "id": "attempt-only-real-capture", "status": "COMPLETED",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }]},
            }]}

        async def capture_order(self, *args, **kwargs):
            self.capture_calls += 1
            raise AssertionError("Recovery must not initiate capture.")

    paypal = PayPal()
    await create_payment_order(proposal_id, owner, paypal_client=paypal)
    async with get_connection() as db:
        await db.execute("DELETE FROM paypal_orders WHERE proposal_id=?", (proposal_id,))
        await db.commit()
    result = await recover_proposal_checkout(proposal_id, owner, paypal_client=paypal)
    assert result.payment_completed is True
    assert paypal.create_calls == 1
    assert paypal.capture_calls == 0
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT status, capture_id FROM paypal_orders WHERE proposal_id=?", (proposal_id,),
        )).fetchone()
    assert row == ("COMPLETED", "attempt-only-real-capture")


@pytest.mark.asyncio
async def test_recovery_keeps_unavailable_and_ambiguous_provider_results_fail_closed() -> None:
    proposal_id = await _proposal("ambiguous-recovery-owner", allow=True)
    owner = "ambiguous-recovery-owner"
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        async def create_order(self, **kwargs):
            return {"order_id": "ambiguous-recovery-order",
                    "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
                    "status": "CREATED"}

    await create_payment_order(proposal_id, owner, paypal_client=PayPal())

    class AmbiguousPayPal:
        async def get_order(self, order_id):
            return {"id": order_id, "status": "PENDING", "purchase_units": []}

    ambiguous = await recover_proposal_checkout(
        proposal_id, owner, paypal_client=AmbiguousPayPal(),
    )
    assert ambiguous.payment_completed is False
    assert ambiguous.status == PayPalOrderStatus.RECONCILIATION_REQUIRED
    assert ambiguous.provider_status is None

    class MismatchedPayPal:
        async def get_order(self, order_id):
            return {"id": order_id, "status": "COMPLETED", "purchase_units": [{
                "reference_id": "different-proposal",
                "amount": {"currency_code": "USD", "value": "10.00"},
                "payments": {"captures": [{
                    "id": "unbound-capture", "status": "COMPLETED",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }]},
            }]}

    mismatch = await recover_proposal_checkout(
        proposal_id, owner, paypal_client=MismatchedPayPal(),
    )
    assert mismatch.payment_completed is False
    assert mismatch.status == PayPalOrderStatus.RECONCILIATION_REQUIRED
    assert mismatch.provider_status == "COMPLETED"

    class UnavailablePayPal:
        async def get_order(self, order_id):
            raise PayPalNetworkError("simulated provider timeout")

    with pytest.raises(PayPalNetworkError):
        await recover_proposal_checkout(proposal_id, owner, paypal_client=UnavailablePayPal())


@pytest.mark.asyncio
async def test_recovery_rejects_foreign_owner_and_concurrent_retries_settle_once() -> None:
    proposal_id = await _proposal("concurrent-recovery-owner", allow=True)
    owner = "concurrent-recovery-owner"
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        capture_calls = 0
        create_calls = 0
        calls = 0
        both_lookups = asyncio.Event()

        async def create_order(self, **kwargs):
            self.create_calls += 1
            return {"order_id": "concurrent-recovery-order",
                    "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
                    "status": "CREATED"}

        async def get_order(self, order_id):
            self.calls += 1
            if self.calls == 2:
                self.both_lookups.set()
            await asyncio.wait_for(self.both_lookups.wait(), timeout=2)
            return {"id": order_id, "status": "COMPLETED", "purchase_units": [{
                "reference_id": proposal_id,
                "amount": {"currency_code": "USD", "value": "10.00"},
                "payments": {"captures": [{
                    "id": "concurrent-real-capture", "status": "COMPLETED",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }]},
            }]}

        async def capture_order(self, *args, **kwargs):
            self.capture_calls += 1
            raise AssertionError("Recovery must not initiate capture.")

    paypal = PayPal()
    await create_payment_order(proposal_id, owner, paypal_client=paypal)
    with pytest.raises(MandateOwnershipError):
        await recover_proposal_checkout(proposal_id, "foreign-owner", paypal_client=paypal)
    results = await asyncio.gather(*[
        recover_proposal_checkout(proposal_id, owner, paypal_client=paypal)
        for _ in range(2)
    ])
    assert all(result.payment_completed for result in results)
    assert paypal.capture_calls == 0
    assert paypal.create_calls == 1
    duplicate = await recover_proposal_checkout(proposal_id, owner, paypal_client=paypal)
    assert duplicate.payment_completed is True
    assert paypal.capture_calls == 0
    assert paypal.create_calls == 1
    events, _ = await list_audit_log_entries(limit=200)
    capture_events = [event for event in events
                      if event.event_type == AuditEventType.PAYMENT_CAPTURED]
    assert len(capture_events) == 1


@pytest.mark.asyncio
async def test_capture_timeout_losing_to_completed_settlement_adds_no_failure_event() -> None:
    from app.db.database import update_paypal_order_captured_in_db, update_proposal_status_in_db

    owner = "capture-timeout-completion-race-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        get_calls = 0

        async def create_order(self, **kwargs):
            return {
                "order_id": "capture-timeout-race-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=opaque",
                "status": "CREATED",
            }

        async def get_order(self, order_id):
            self.get_calls += 1
            return {
                "id": order_id, "status": "APPROVED", "purchase_units": [{
                    "reference_id": proposal_id,
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }],
            }

        async def capture_order(self, order_id, capture_request_id):
            # Deterministically model a concurrent verifier persisting the real
            # provider completion before this request observes its timeout.
            async with get_connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await update_paypal_order_captured_in_db(
                    order_id, "race-winner-capture", capture_request_id,
                    datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
                    db=db,
                )
                proposal = await get_proposal_from_db(proposal_id, db=db)
                assert proposal is not None
                await update_proposal_status_in_db(
                    proposal_id, ProposalStatus.SETTLED,
                    expected_current_status=proposal.status, db=db,
                )
                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_CAPTURED,
                    entity_type=AuditEntityType.PAYMENT, entity_id=order_id,
                    actor=AuditActor.SYSTEM,
                    payload={"order_id": order_id, "capture_id": "race-winner-capture",
                             "proposal_id": proposal_id, "total": "10.00",
                             "currency": "USD"}, db=db,
                )
                await db.commit()
            raise PayPalNetworkError("simulated stale timeout after concurrent completion")

    paypal = PayPal()
    await create_payment_order(proposal_id, owner, paypal_client=paypal)
    result = await capture_payment_order(
        "capture-timeout-race-order", owner, paypal_client=paypal,
    )
    assert result.status == PayPalOrderStatus.COMPLETED
    assert result.capture_id == "race-winner-capture"
    async with get_connection() as db:
        events = [row[0] for row in await (await db.execute(
            "SELECT event_type FROM audit_logs WHERE entity_id=? ORDER BY sequence_number",
            ("capture-timeout-race-order",),
        )).fetchall()]
    assert events.count("PAYMENT_CAPTURE_ATTEMPTED") == 1
    assert events.count("PAYMENT_CAPTURED") == 1
    assert "PAYMENT_FAILED" not in events


@pytest.mark.asyncio
async def test_stale_return_snapshot_cannot_reopen_capture_reservation(monkeypatch) -> None:
    from app.services import payment_service

    owner = "stale-return-capture-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    class PayPal:
        async def create_order(self, **kwargs):
            return {
                "order_id": "return-race-order",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=opaque",
                "status": "CREATED",
            }

        async def get_order(self, order_id):
            return {
                "id": order_id, "status": "APPROVED", "purchase_units": [{
                    "reference_id": proposal_id,
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }],
            }

    await create_payment_order(proposal_id, owner, paypal_client=PayPal())
    order = await get_paypal_order_from_db("return-race-order")
    assert order is not None
    async with get_connection() as db:
        await db.execute(
            "UPDATE paypal_orders SET status='CAPTURING' WHERE order_id=?",
            ("return-race-order",),
        )
        await db.commit()

    original_get_order = payment_service.get_paypal_order_from_db

    async def stale_initial_read(order_id, db=None):
        if db is None:
            return order.model_copy(update={"status": PayPalOrderStatus.CREATED})
        return await original_get_order(order_id, db=db)

    monkeypatch.setattr(payment_service, "get_paypal_order_from_db", stale_initial_read)
    result = await verify_payment_return(
        proposal_id, "return-race-order", owner, paypal_client=PayPal()
    )
    assert result.status == PayPalOrderStatus.CAPTURING
    async with get_connection() as db:
        status = await (await db.execute(
            "SELECT status FROM paypal_orders WHERE order_id=?", ("return-race-order",),
        )).fetchone()
    assert status == ("CAPTURING",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_level", "MEDIUM"),
        ("finding_codes_json", '["PRICE_INCONSISTENCY"]'),
        ("affected_fields_json", '["unit_price"]'),
        ("assessment_id", "ia_new_identity"),
        ("policy_version", "policy-new"),
        ("schema_version", "schema-new"),
        ("prompt_version", "prompt-new"),
        ("model_identifier", "model-new"),
    ],
)
@pytest.mark.asyncio
async def test_assessment_field_changes_invalidate_old_consent(field: str, value: str) -> None:
    owner = f"assessment-change-{field}"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)
    await _change_assessment(proposal_id, **{field: value})
    with pytest.raises((PurchaseConsentRequiredError, PurchaseConsentDeniedError,
                        IntentAuditorUnavailable)):
        await require_current_purchase_consent(proposal_id, owner)


@pytest.mark.parametrize("status", ["FAILED", "STALE"])
@pytest.mark.asyncio
async def test_failed_or_stale_assessment_cannot_authorize_consent(status: str) -> None:
    owner = f"assessment-status-{status}"
    proposal_id = await _proposal(owner, allow=True)
    await _change_assessment(proposal_id, status=status)
    with pytest.raises(IntentAuditorUnavailable):
        await record_purchase_consent(proposal_id, owner)
    proposal = await get_proposal_from_db(proposal_id)
    assert proposal is not None and proposal.status == ProposalStatus.EVALUATED
    async with get_connection() as db:
        count = await (await db.execute(
            "SELECT COUNT(*) FROM purchase_consents WHERE proposal_id = ?",
            (proposal_id,),
        )).fetchone()
        assert count[0] == 0


@pytest.mark.asyncio
async def test_missing_assessment_fails_closed_for_consent() -> None:
    owner = "missing-assessment-owner"
    proposal_id = await _proposal(owner, allow=True)
    async with get_connection() as db:
        await db.execute("DELETE FROM intent_assessments WHERE proposal_id = ?", (proposal_id,))
        await db.commit()
    with pytest.raises(IntentAuditorUnavailable):
        await record_purchase_consent(proposal_id, owner)


@pytest.mark.asyncio
async def test_block_wrong_owner_and_foreign_ids_fail_closed(
    client: httpx.AsyncClient,
) -> None:
    headers, owner = await _auth(client)
    foreign_headers, _ = await _auth(client)
    blocked_id = await _proposal(owner, allow=True, hard_block=True)
    blocked = await client.post(
        f"/api/v1/proposals/{blocked_id}/purchase-consent",
        json={"confirm_purchase": True},
        headers=headers,
    )
    assert blocked.status_code == 409

    other_id = await _proposal(owner, allow=True)
    foreign = await client.post(
        f"/api/v1/proposals/{other_id}/purchase-consent",
        json={"confirm_purchase": True},
        headers=foreign_headers,
    )
    missing = await client.post(
        "/api/v1/proposals/no-such-proposal/purchase-consent",
        json={"confirm_purchase": True},
        headers=foreign_headers,
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


@pytest.mark.asyncio
async def test_idempotent_and_concurrent_consent_append_once(client: httpx.AsyncClient) -> None:
    headers, owner = await _auth(client)
    proposal_id = await _proposal(owner, allow=True)

    results = await asyncio.gather(
        record_purchase_consent(proposal_id, owner),
        record_purchase_consent(proposal_id, owner),
    )
    assert {result.policy_decision for result in results} == {"ALLOW"}
    assert sorted(result.idempotent for result in results) == [False, True]
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM purchase_consents WHERE proposal_id = ?", (proposal_id,)
        )
        assert (await cursor.fetchone())[0] == 1
    _, audit_count = await list_audit_log_entries(
        event_type=AuditEventType.PURCHASE_CONSENT_RECORDED
    )
    assert audit_count == 1


@pytest.mark.asyncio
async def test_consent_and_audit_roll_back_together(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = "rollback-consent-owner"
    proposal_id = await _proposal(owner, allow=True)

    async def fail_audit(**kwargs):
        raise RuntimeError("controlled audit failure")

    monkeypatch.setattr(AuditService, "append_event", fail_audit)
    with pytest.raises(RuntimeError, match="controlled audit failure"):
        await record_purchase_consent(proposal_id, owner)
    proposal = await get_proposal_from_db(proposal_id)
    assert proposal is not None and proposal.status == ProposalStatus.EVALUATED
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM purchase_consents WHERE proposal_id = ?", (proposal_id,)
        )
        assert (await cursor.fetchone())[0] == 0
        cursor = await db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE entity_id = ? "
            "AND event_type = 'PURCHASE_CONSENT_RECORDED'",
            (proposal_id,),
        )
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_changed_proposal_and_mandate_bindings_fail_closed() -> None:
    cases = ("total", "currency", "mandate")
    for index, changed_field in enumerate(cases):
        owner = f"binding-owner-{index}"
        proposal_id = await _proposal(owner, allow=True)
        await record_purchase_consent(proposal_id, owner)
        proposal = await get_proposal_from_db(proposal_id)
        assert proposal is not None
        async with get_connection() as db:
            if changed_field == "total":
                await db.execute(
                    "UPDATE proposals SET total = ? WHERE proposal_id = ?",
                    ("11.00", proposal_id),
                )
            elif changed_field == "currency":
                await db.execute(
                    "UPDATE proposals SET currency = ? WHERE proposal_id = ?",
                    ("EUR", proposal_id),
                )
            else:
                await db.execute(
                    "UPDATE mandates SET allowed_merchants = ? WHERE mandate_id = ?",
                    ('["attacker.example"]', proposal.mandate_id),
                )
            await db.commit()

        if changed_field in {"total", "currency"}:
            with pytest.raises(ProposalTamperedError):
                await require_current_purchase_consent(proposal_id, owner)
        else:
            with pytest.raises(PurchaseConsentDeniedError, match="integrity"):
                await require_current_purchase_consent(proposal_id, owner)


@pytest.mark.asyncio
async def test_changed_policy_and_legacy_approved_without_consent_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import purchase_consent_service as service

    owner = "legacy-consent-owner"
    proposal_id = await _proposal(owner, allow=True)
    await record_purchase_consent(proposal_id, owner)

    original_hash = service.policy_snapshot_hash
    monkeypatch.setattr(service, "policy_snapshot_hash", lambda decision: "f" * 64)
    with pytest.raises(PurchaseConsentRequiredError):
        await require_current_purchase_consent(proposal_id, owner)
    monkeypatch.setattr(service, "policy_snapshot_hash", original_hash)

    legacy_id = await _proposal("legacy-no-consent", allow=True)
    async with get_connection() as db:
        await db.execute(
            "UPDATE proposals SET status = 'APPROVED' WHERE proposal_id = ?", (legacy_id,)
        )
        await db.commit()
    with pytest.raises(PurchaseConsentRequiredError):
        await require_current_purchase_consent(legacy_id, "legacy-no-consent")

    proposal = await get_proposal_from_db(legacy_id)
    assert proposal is not None
    class PayPal:
        create_calls = 0
        get_calls = 0

        async def create_order(self, **kwargs):
            self.create_calls += 1
            raise AssertionError("Legacy consent must fail before PayPal order creation")

        async def get_order(self, order_id):
            self.get_calls += 1
            raise AssertionError("Legacy consent must fail before PayPal lookup")

    paypal = PayPal()
    with pytest.raises(PurchaseConsentRequiredError):
        await create_payment_order(legacy_id, "legacy-no-consent", paypal_client=paypal)
    assert paypal.create_calls == 0

    now = datetime.now(timezone.utc)
    await insert_paypal_order_in_db(PayPalOrderRecord(
        order_id="legacy-order-no-consent",
        proposal_id=legacy_id,
        mandate_id=proposal.mandate_id,
        owner_user_id="legacy-no-consent",
        status=PayPalOrderStatus.CREATED,
        currency=proposal.currency,
        total_amount=proposal.total,
        subtotal=proposal.subtotal,
        tax=proposal.tax,
        shipping=proposal.shipping,
        other_charges=proposal.other_charges,
        approval_url="https://www.sandbox.paypal.com/checkoutnow?token=opaque",
        paypal_request_id="legacy-order-request",
        created_at=now,
        updated_at=now,
    ))
    with pytest.raises(PurchaseConsentRequiredError):
        await capture_payment_order(
            "legacy-order-no-consent", "legacy-no-consent", paypal_client=paypal
        )
    assert paypal.get_calls == 0


@pytest.mark.asyncio
async def test_real_v8_database_upgrades_without_backfill(monkeypatch, tmp_path) -> None:
    """Build an actual v8 schema, then run the real v9-v11 migration path."""
    from app.db.database import init_db, get_paypal_order_from_db

    database_path = tmp_path / "legacy-v8.db"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{database_path}")
    await init_db(_target_version=8)
    async with get_connection() as db:
        assert (await (await db.execute(
            "SELECT MAX(version) FROM schema_version"
        )).fetchone())[0] == 8
        assert await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='purchase_consents'"
        )).fetchone() is None

    approved_id = await _proposal("historical-approved-owner", allow=True)
    settled_id = await _proposal("historical-settled-owner", allow=True)
    approved = await get_proposal_from_db(approved_id)
    settled = await get_proposal_from_db(settled_id)
    assert approved is not None and settled is not None
    legacy_created = datetime.now(timezone.utc).isoformat()
    legacy_expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    async with get_connection() as db:
        await db.execute(
            "UPDATE proposals SET status='APPROVED' WHERE proposal_id = ?", (approved_id,)
        )
        await db.execute(
            "UPDATE proposals SET status='SETTLED' WHERE proposal_id = ?", (settled_id,)
        )
        await db.execute(
            "INSERT INTO approval_tokens "
            "(token_hash, proposal_id, expires_at, created_at, consumed_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            ("legacy-v8-token-hash", approved_id, legacy_expiry, legacy_created),
        )
        await db.commit()
    historical_order = PayPalOrderRecord(
        order_id="legacy-v8-settled-order",
        proposal_id=settled_id,
        mandate_id=settled.mandate_id,
        owner_user_id="historical-settled-owner",
        status=PayPalOrderStatus.COMPLETED,
        currency=settled.currency,
        total_amount=settled.total,
        subtotal=settled.subtotal,
        tax=settled.tax,
        shipping=settled.shipping,
        other_charges=settled.other_charges,
        approval_url="https://www.sandbox.paypal.com/checkoutnow?token=legacy",
        paypal_request_id="legacy-v8-request",
        capture_id="legacy-v8-capture",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        captured_at=datetime.now(timezone.utc),
    )
    await insert_paypal_order_in_db(historical_order)

    # A v8 session is independent of the later consent migrations, but retain a
    # real persisted row so the migration's full-row preservation is exercised.
    historical_session = (
        "legacy-v8-session-hash", "historical-settled-owner", legacy_created,
        legacy_expiry, None,
    )
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO user_sessions "
            "(session_token_hash, user_id, created_at, expires_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?)", historical_session,
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        async def snapshot(query: str, params: tuple = ()) -> tuple[tuple, ...]:
            rows = await (await db.execute(query, params)).fetchall()
            return tuple(tuple(row) for row in rows)

        historical_session_before = await snapshot(
            "SELECT * FROM user_sessions WHERE session_token_hash = ?",
            (historical_session[0],),
        )
        historical_audit_before = await snapshot(
            "SELECT * FROM audit_logs ORDER BY sequence_number"
        )
        historical_checkpoints_before = await snapshot(
            "SELECT * FROM audit_checkpoints ORDER BY sequence_number"
        )
        historical_proposals_before = await snapshot(
            "SELECT * FROM proposals WHERE proposal_id IN (?, ?) ORDER BY proposal_id",
            (approved_id, settled_id),
        )
        historical_token_before = await snapshot(
            "SELECT token_hash, proposal_id, expires_at, created_at, consumed_at "
            "FROM approval_tokens WHERE token_hash = ?",
            ("legacy-v8-token-hash",),
        )
        historical_orders_before = await snapshot(
            "SELECT * FROM paypal_orders WHERE order_id = ?",
            (historical_order.order_id,),
        )
    pre_migration_audit_check = await AuditService.verify_chain()
    assert pre_migration_audit_check.valid is True

    await init_db(_target_version=9)
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        assert (await (await db.execute(
            "SELECT MAX(version) FROM schema_version"
        )).fetchone())[0] == 9
        assert tuple(await (await db.execute(
            "SELECT owner_user_id, content_hash FROM approval_tokens "
            "WHERE token_hash = 'legacy-v8-token-hash'"
        )).fetchone()) == (None, None)
        assert (await (await db.execute(
            "SELECT COUNT(*) FROM purchase_consents"
        )).fetchone())[0] == 0
        db.row_factory = aiosqlite.Row
        assert tuple(await (await db.execute(
            "SELECT * FROM user_sessions WHERE session_token_hash = ?",
            (historical_session[0],),
        )).fetchone()) == historical_session_before[0]
        assert tuple(tuple(row) for row in await (await db.execute(
            "SELECT * FROM audit_logs ORDER BY sequence_number"
        )).fetchall()) == historical_audit_before
        assert tuple(tuple(row) for row in await (await db.execute(
            "SELECT * FROM audit_checkpoints ORDER BY sequence_number"
        )).fetchall()) == historical_checkpoints_before

    await init_db()
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        assert (await (await db.execute(
            "SELECT MAX(version) FROM schema_version"
        )).fetchone())[0] == 11
        assert await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paypal_order_attempts'"
        )).fetchone() is not None
        assert (await (await db.execute(
            "SELECT COUNT(*) FROM paypal_order_attempts"
        )).fetchone())[0] == 0
        token_columns = {row["name"] for row in await (await db.execute(
            "PRAGMA table_info(approval_tokens)"
        )).fetchall()}
        consent_columns = {row["name"] for row in await (await db.execute(
            "PRAGMA table_info(purchase_consents)"
        )).fetchall()}
        token_column_info = {row["name"]: row for row in await (await db.execute(
            "PRAGMA table_info(approval_tokens)"
        )).fetchall()}
        consent_column_info = {row["name"]: row for row in await (await db.execute(
            "PRAGMA table_info(purchase_consents)"
        )).fetchall()}
        assert {"owner_user_id", "content_hash", "assessment_binding"} <= token_columns
        assert "assessment_binding" in consent_columns
        assert token_column_info["assessment_binding"]["notnull"] == 0
        assert consent_column_info["assessment_binding"]["notnull"] == 0
        token_row = await (await db.execute(
            "SELECT owner_user_id, content_hash, assessment_binding "
            "FROM approval_tokens WHERE token_hash = 'legacy-v8-token-hash'"
        )).fetchone()
        assert tuple(token_row) == (None, None, None)
        assert (await (await db.execute(
            "SELECT COUNT(*) FROM purchase_consents"
        )).fetchone())[0] == 0
        assert tuple(await (await db.execute(
            "SELECT * FROM user_sessions WHERE session_token_hash = ?",
            (historical_session[0],),
        )).fetchone()) == historical_session_before[0]
        assert tuple(tuple(row) for row in await (await db.execute(
            "SELECT * FROM audit_logs ORDER BY sequence_number"
        )).fetchall()) == historical_audit_before
        assert tuple(tuple(row) for row in await (await db.execute(
            "SELECT * FROM audit_checkpoints ORDER BY sequence_number"
        )).fetchall()) == historical_checkpoints_before
        assert tuple(tuple(row) for row in await (await db.execute(
            "SELECT * FROM proposals WHERE proposal_id IN (?, ?) ORDER BY proposal_id",
            (approved_id, settled_id),
        )).fetchall()) == historical_proposals_before
        assert tuple(tuple(row) for row in await (await db.execute(
            "SELECT token_hash, proposal_id, expires_at, created_at, consumed_at "
            "FROM approval_tokens WHERE token_hash = ?",
            ("legacy-v8-token-hash",),
        )).fetchall()) == historical_token_before
        assert tuple(tuple(row) for row in await (await db.execute(
            "SELECT * FROM paypal_orders WHERE order_id = ?",
            (historical_order.order_id,),
        )).fetchall()) == historical_orders_before

    post_migration_audit_check = await AuditService.verify_chain()
    assert post_migration_audit_check.valid is True

    assert (await get_proposal_from_db(approved_id)).status == ProposalStatus.APPROVED
    assert (await get_proposal_from_db(settled_id)).status == ProposalStatus.SETTLED
    with pytest.raises(PurchaseConsentRequiredError):
        await require_current_purchase_consent(
            approved_id, "historical-approved-owner"
        )
    stored_order = await get_paypal_order_from_db("legacy-v8-settled-order")
    assert stored_order == historical_order

    # An upgraded database can create a new, fully bound consent normally.
    new_id = await _proposal("post-upgrade-owner", allow=True)
    await record_purchase_consent(new_id, "post-upgrade-owner")
    async with get_connection() as db:
        assert (await (await db.execute(
            "SELECT COUNT(*) FROM purchase_consents WHERE proposal_id = ? "
            "AND assessment_binding IS NOT NULL", (new_id,),
        )).fetchone())[0] == 1
