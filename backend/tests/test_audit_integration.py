"""Integration tests verifying end-to-end audit chain integrity across all lifecycle events."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.database import list_audit_log_entries
from app.schemas.audit import AuditEventType
from app.schemas.mandate import MandateCreate
from app.schemas.proposal import ProposalCreate
from app.services.approval_service import consume_approval_token, issue_approval_token
from app.services.audit_service import AuditService
from app.services.kill_switch_service import disengage_kill_switch, engage_kill_switch
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.proposal_service import create_proposal, evaluate_proposal_service


@pytest.mark.asyncio
async def test_full_lifecycle_audit_integration_and_verification() -> None:
    """Execute all lifecycle operations and confirm complete hash-chain cryptographic validity."""
    # 1. Mandate creation -> MANDATE_CREATED
    mandate_data = MandateCreate(
        max_total_budget=Decimal("300.00"),
        currency="USD",
        allowed_merchants=["bhphotovideo.com"],
        allowed_categories=["cameras"],
        max_item_count=3,
        max_quantity_per_item=1,
        max_price_per_item=Decimal("150.00"),
        allow_subscriptions=False,
        require_user_approval=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    mandate = await create_draft_mandate(mandate_data)

    # 2. Mandate confirm & sign -> MANDATE_SIGNED
    await confirm_mandate(mandate.mandate_id)
    await sign_mandate(mandate.mandate_id)
    await activate_mandate(mandate.mandate_id)

    # 3. Proposal creation -> PROPOSAL_CREATED
    proposal_data = ProposalCreate(
        mandate_id=mandate.mandate_id,
        product_id="sku-lens-50mm",
        product_name="50mm f/1.8 Prime Lens",
        merchant="bhphotovideo.com",
        category="cameras",
        quantity=1,
        unit_price=Decimal("199.99"),  # Exceeds max_price_per_item $150 -> REQUIRE_APPROVAL
        currency="USD",
        shipping=Decimal("10.00"),
        tax=Decimal("16.00"),
        other_charges=Decimal("0.00"),
        product_source="https://bhphotovideo.com/lens",
    )
    proposal, _ = await create_proposal(proposal_data)

    # 4. Proposal evaluation -> PROPOSAL_EVALUATED
    decision = await evaluate_proposal_service(proposal.proposal_id)
    assert decision.decision.value == "REQUIRE_APPROVAL"

    # 5. One-time exception approval token issuance -> EXCEPTION_APPROVED (action=token_issued)
    token_meta = await issue_approval_token(
        proposal_id=proposal.proposal_id,
        user_session_id="user_photographer_session",
    )

    # 6. One-time exception approval token consumption -> EXCEPTION_APPROVED
    success, reason = await consume_approval_token(
        proposal_id=proposal.proposal_id,
        token=token_meta["token"],
        user_session_id="user_photographer_session",
        issued_at_iso=token_meta["issued_at"],
        nonce=token_meta["nonce"],
    )
    assert success is True

    # 7. Kill switch engage and disengage -> KILL_SWITCH_ENGAGED & KILL_SWITCH_DISENGAGED
    await engage_kill_switch("sec_officer", "Routine lockdown drill")
    await disengage_kill_switch("sec_officer", "Drill complete")

    # 8. Cryptographic verification of the entire hash chain
    verify_result = await AuditService.verify_chain()
    assert verify_result.valid is True
    assert verify_result.total_records >= 7  # Genesis + at least 6 lifecycle events
    assert verify_result.error_message is None
    assert verify_result.anomalies == []

    # 9. Verify event presence in audit log
    entries, total = await list_audit_log_entries(limit=100)
    event_types = [e.event_type for e in entries]

    assert AuditEventType.GENESIS.value in event_types
    assert AuditEventType.MANDATE_CREATED.value in event_types
    assert AuditEventType.MANDATE_SIGNED.value in event_types
    assert AuditEventType.PROPOSAL_CREATED.value in event_types
    assert AuditEventType.PROPOSAL_EVALUATED.value in event_types
    assert AuditEventType.EXCEPTION_APPROVED.value in event_types
    assert AuditEventType.KILL_SWITCH_ENGAGED.value in event_types
    assert AuditEventType.KILL_SWITCH_DISENGAGED.value in event_types


@pytest.mark.asyncio
async def test_audit_payload_contains_no_floats_or_decimals() -> None:
    """Ensure all logged payloads contain only canonical JSON serializable strings and ints."""
    entries, total = await list_audit_log_entries(limit=100)
    for entry in entries:
        _assert_no_imprecise_types(entry.payload)


def _assert_no_imprecise_types(obj: object) -> None:
    if isinstance(obj, float):
        pytest.fail(f"Float found in audit payload: {obj}")
    if isinstance(obj, Decimal):
        pytest.fail(f"Decimal found in audit payload: {obj}")
    if isinstance(obj, dict):
        for v in obj.values():
            _assert_no_imprecise_types(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _assert_no_imprecise_types(item)
