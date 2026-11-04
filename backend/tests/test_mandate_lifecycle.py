"""Tests for mandate lifecycle state transitions and authorization safeguards."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.schemas.mandate import MandateCreate, MandateStatus
from app.services.mandate_service import (
    InvalidStateTransitionError,
    MandateExpiredError,
    MandateNotFoundError,
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    get_mandate,
    revoke_mandate,
    sign_mandate,
    verify_mandate,
)

pytestmark = pytest.mark.asyncio

VALID_SECRET = "leash-test-secret-signing-key-32-bytes-minimum!!"


def _create_sample_payload(hours_valid: int = 24) -> MandateCreate:
    now = datetime.now(timezone.utc)
    return MandateCreate(
        max_total_budget=Decimal("40.00"),
        currency="USD",
        allowed_merchants=["amazon.com", "target.com"],
        allowed_categories=["gifts", "books"],
        max_item_count=2,
        max_quantity_per_item=1,
        max_price_per_item=Decimal("25.00"),
        allow_subscriptions=False,
        require_user_approval=True,
        expires_at=now + timedelta(hours=hours_valid),
    )


async def test_full_mandate_lifecycle_success() -> None:
    """Test standard progression: DRAFT -> CONFIRMED -> SIGNED -> ACTIVE."""
    payload = _create_sample_payload()

    # 1. Create Draft
    draft = await create_draft_mandate(payload)
    assert draft.status == MandateStatus.DRAFT
    assert draft.signature is None

    # 2. Confirm
    confirmed = await confirm_mandate(draft.mandate_id)
    assert confirmed.status == MandateStatus.CONFIRMED

    # 3. Sign
    signed = await sign_mandate(draft.mandate_id, secret=VALID_SECRET)
    assert signed.status == MandateStatus.SIGNED
    assert signed.signature is not None
    assert signed.signature_algorithm == "HMAC-SHA256"

    # 4. Activate
    active = await activate_mandate(draft.mandate_id, secret=VALID_SECRET)
    assert active.status == MandateStatus.ACTIVE

    # 5. Verify Active Mandate
    verification = await verify_mandate(draft.mandate_id, secret=VALID_SECRET)
    assert verification.valid is True
    assert verification.status == MandateStatus.ACTIVE
    assert verification.is_expired is False
    assert verification.is_tampered is False
    assert verification.reason is None


async def test_signing_unconfirmed_mandate_rejected() -> None:
    """Test that a DRAFT mandate cannot be signed without explicit user confirmation."""
    payload = _create_sample_payload()
    draft = await create_draft_mandate(payload)

    with pytest.raises(InvalidStateTransitionError) as exc_info:
        await sign_mandate(draft.mandate_id, secret=VALID_SECRET)
    assert "explicitly confirmed" in str(exc_info.value)


async def test_activating_unconfirmed_or_unsigned_mandate_rejected() -> None:
    """Test that activating a DRAFT or CONFIRMED mandate is rejected."""
    payload = _create_sample_payload()
    draft = await create_draft_mandate(payload)

    # Cannot activate draft
    with pytest.raises(InvalidStateTransitionError):
        await activate_mandate(draft.mandate_id, secret=VALID_SECRET)

    # Cannot activate confirmed without signing
    await confirm_mandate(draft.mandate_id)
    with pytest.raises(InvalidStateTransitionError):
        await activate_mandate(draft.mandate_id, secret=VALID_SECRET)


async def test_expired_mandate_cannot_progress() -> None:
    """Test that expired mandates cannot be confirmed, signed, or activated."""
    now = datetime.now(timezone.utc)
    # Mandate that expires immediately (1 second from now)
    payload = MandateCreate(
        max_total_budget=Decimal("40.00"),
        currency="USD",
        allowed_merchants=["amazon.com"],
        allowed_categories=["books"],
        expires_at=now + timedelta(seconds=1),
    )
    draft = await create_draft_mandate(payload)

    # Artificially update expiration into the past in the database to test expiration safeguard
    import aiosqlite
    from app.db.database import get_db_path

    past_iso = (now - timedelta(minutes=1)).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE mandates SET expires_at = ? WHERE mandate_id = ?",
            (past_iso, draft.mandate_id),
        )
        await db.commit()

    # Attempting confirmation must raise MandateExpiredError
    with pytest.raises(MandateExpiredError):
        await confirm_mandate(draft.mandate_id)


async def test_expired_mandate_verification_fails() -> None:
    """Test that an active mandate that has expired fails verification."""
    payload = _create_sample_payload(hours_valid=1)
    draft = await create_draft_mandate(payload)
    await confirm_mandate(draft.mandate_id)
    await sign_mandate(draft.mandate_id, secret=VALID_SECRET)
    await activate_mandate(draft.mandate_id, secret=VALID_SECRET)

    # Artificially expire the active mandate in the database with matching signature
    import aiosqlite
    from app.core.security import sign_payload
    from app.db.database import get_db_path

    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE mandates SET expires_at = ? WHERE mandate_id = ?",
            (past_iso, draft.mandate_id),
        )
        await db.commit()

    mandate = await get_mandate(draft.mandate_id)
    valid_expired_sig = sign_payload(mandate.canonical_payload(), VALID_SECRET)
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE mandates SET signature = ? WHERE mandate_id = ?",
            (valid_expired_sig, draft.mandate_id),
        )
        await db.commit()

    result = await verify_mandate(draft.mandate_id, secret=VALID_SECRET)
    assert result.valid is False
    assert result.is_expired is True
    assert "expired" in result.reason.lower()


async def test_revocation_lifecycle() -> None:
    """Test revoking a mandate from any state and ensuring it is permanently terminal."""
    payload = _create_sample_payload()
    draft = await create_draft_mandate(payload)
    await confirm_mandate(draft.mandate_id)
    await sign_mandate(draft.mandate_id, secret=VALID_SECRET)
    await activate_mandate(draft.mandate_id, secret=VALID_SECRET)

    # Revoke
    revoked = await revoke_mandate(draft.mandate_id)
    assert revoked.status == MandateStatus.REVOKED

    # Cannot re-activate a revoked mandate
    with pytest.raises(InvalidStateTransitionError):
        await activate_mandate(draft.mandate_id, secret=VALID_SECRET)

    # Cannot re-revoke
    with pytest.raises(InvalidStateTransitionError):
        await revoke_mandate(draft.mandate_id)

    # Verification must report invalid due to revocation
    result = await verify_mandate(draft.mandate_id, secret=VALID_SECRET)
    assert result.valid is False
    assert result.status == MandateStatus.REVOKED
    assert "revoked" in result.reason.lower()


async def test_get_nonexistent_mandate_raises_not_found() -> None:
    """Test that querying a nonexistent mandate raises MandateNotFoundError."""
    with pytest.raises(MandateNotFoundError):
        await get_mandate("man_nonexistent_id")
