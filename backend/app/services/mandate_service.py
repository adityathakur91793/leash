"""Mandate service for lifecycle transitions, signing, and verification."""

import uuid
from datetime import datetime, timezone

import aiosqlite

from app.core.config import settings
from app.core.security import (
    canonicalize_mandate_payload,
    sign_payload,
    validate_signing_secret,
    verify_signature,
)
from app.db.database import (
    get_mandate_from_db,
    list_mandates_from_db,
    save_mandate_to_db,
)
from app.schemas.mandate import (
    Mandate,
    MandateCreate,
    MandateStatus,
    MandateVerificationResponse,
)


class MandateNotFoundError(Exception):
    """Raised when a mandate cannot be found."""


class InvalidStateTransitionError(Exception):
    """Raised when an illegal lifecycle state transition is attempted."""


class MandateExpiredError(Exception):
    """Raised when an operation is attempted on an expired mandate."""


class MandateTamperedError(Exception):
    """Raised when signature verification detects mandate tampering."""


async def _expire_if_needed(
    mandate: Mandate, db: aiosqlite.Connection | None = None
) -> Mandate:
    """Transition a mandate to EXPIRED when its validity window has closed."""
    now = datetime.now(timezone.utc)
    if now >= mandate.expires_at and mandate.status not in (
        MandateStatus.EXPIRED,
        MandateStatus.REVOKED,
    ):
        mandate.status = MandateStatus.EXPIRED
        await save_mandate_to_db(mandate, db=db)
    return mandate


async def create_draft_mandate(data: MandateCreate) -> Mandate:
    """Create and persist a new mandate in DRAFT status."""
    created = datetime.now(timezone.utc)

    mandate = Mandate(
        mandate_id=f"man_{uuid.uuid4().hex}",
        schema_version="1.0",
        status=MandateStatus.DRAFT,
        max_total_budget=data.max_total_budget,
        currency=data.currency,
        allowed_merchants=data.allowed_merchants,
        allowed_categories=data.allowed_categories,
        max_item_count=data.max_item_count,
        max_quantity_per_item=data.max_quantity_per_item,
        max_price_per_item=data.max_price_per_item,
        allow_subscriptions=data.allow_subscriptions,
        require_user_approval=data.require_user_approval,
        created_at=created,
        expires_at=data.expires_at,
        signature=None,
        signed_at=None,
        signature_algorithm=None,
    )

    await save_mandate_to_db(mandate)
    return mandate


async def get_mandate(
    mandate_id: str, db: aiosqlite.Connection | None = None
) -> Mandate:
    """Retrieve a mandate by ID, evaluating expiration if necessary."""
    mandate = await get_mandate_from_db(mandate_id, db=db)
    if mandate is None:
        raise MandateNotFoundError(f"Mandate with ID '{mandate_id}' was not found.")

    return await _expire_if_needed(mandate, db=db)


async def confirm_mandate(mandate_id: str) -> Mandate:
    """Confirm a DRAFT mandate, locking its permissions in preparation for signing."""
    mandate = await get_mandate(mandate_id)

    now = datetime.now(timezone.utc)
    if now >= mandate.expires_at:
        mandate.status = MandateStatus.EXPIRED
        await save_mandate_to_db(mandate)
        raise MandateExpiredError(f"Cannot confirm mandate '{mandate_id}': it has expired.")

    if mandate.status != MandateStatus.DRAFT:
        raise InvalidStateTransitionError(
            f"Cannot confirm mandate in '{mandate.status.value}' state. Must be DRAFT."
        )

    mandate.status = MandateStatus.CONFIRMED
    await save_mandate_to_db(mandate)
    return mandate


async def sign_mandate(mandate_id: str, secret: str | None = None) -> Mandate:
    """Sign a CONFIRMED mandate using HMAC-SHA256."""
    mandate = await get_mandate(mandate_id)

    now = datetime.now(timezone.utc)
    if now >= mandate.expires_at:
        mandate.status = MandateStatus.EXPIRED
        await save_mandate_to_db(mandate)
        raise MandateExpiredError(f"Cannot sign mandate '{mandate_id}': it has expired.")

    if mandate.status == MandateStatus.DRAFT:
        raise InvalidStateTransitionError(
            "Mandate must be explicitly confirmed by the user before it can be signed."
        )
    if mandate.status != MandateStatus.CONFIRMED:
        raise InvalidStateTransitionError(
            f"Cannot sign mandate in '{mandate.status.value}' state. Must be CONFIRMED."
        )

    signing_key = secret if secret is not None else settings.MANDATE_SIGNING_KEY
    validate_signing_secret(signing_key)

    canonical_payload = mandate.canonical_payload()
    signature = sign_payload(canonical_payload, signing_key)

    mandate.status = MandateStatus.SIGNED
    mandate.signature = signature
    mandate.signed_at = now
    mandate.signature_algorithm = settings.MANDATE_KEY_ALGORITHM

    await save_mandate_to_db(mandate)
    return mandate


async def activate_mandate(mandate_id: str, secret: str | None = None) -> Mandate:
    """Transition a SIGNED mandate to ACTIVE after verifying signature integrity."""
    mandate = await get_mandate(mandate_id)

    now = datetime.now(timezone.utc)
    if now >= mandate.expires_at:
        mandate.status = MandateStatus.EXPIRED
        await save_mandate_to_db(mandate)
        raise MandateExpiredError(f"Cannot activate mandate '{mandate_id}': it has expired.")

    if mandate.status != MandateStatus.SIGNED:
        raise InvalidStateTransitionError(
            f"Cannot activate mandate in '{mandate.status.value}' state. Must be SIGNED."
        )

    signing_key = secret if secret is not None else settings.MANDATE_SIGNING_KEY
    validate_signing_secret(signing_key)

    canonical_payload = mandate.canonical_payload()
    if not verify_signature(canonical_payload, mandate.signature, signing_key):
        raise MandateTamperedError("Mandate signature verification failed. Cannot activate.")

    mandate.status = MandateStatus.ACTIVE
    await save_mandate_to_db(mandate)
    return mandate


async def revoke_mandate(mandate_id: str) -> Mandate:
    """Revoke a mandate immediately. Revocation is permanent and terminal."""
    mandate = await get_mandate(mandate_id)

    if mandate.status == MandateStatus.REVOKED:
        raise InvalidStateTransitionError(f"Mandate '{mandate_id}' is already revoked.")

    mandate.status = MandateStatus.REVOKED
    await save_mandate_to_db(mandate)
    return mandate


async def verify_mandate(
    mandate_id: str, secret: str | None = None
) -> MandateVerificationResponse:
    """Verify mandate integrity, signature validity, expiration, and lifecycle state."""
    mandate = await get_mandate(mandate_id)

    signing_key = secret if secret is not None else settings.MANDATE_SIGNING_KEY
    validate_signing_secret(signing_key)

    now = datetime.now(timezone.utc)
    is_expired = now >= mandate.expires_at

    if mandate.signature is None:
        return MandateVerificationResponse(
            mandate_id=mandate.mandate_id,
            valid=False,
            status=mandate.status,
            is_expired=is_expired,
            is_tampered=False,
            reason="Mandate has not been cryptographically signed.",
        )

    canonical_payload = mandate.canonical_payload()
    is_valid_sig = verify_signature(canonical_payload, mandate.signature, signing_key)

    if not is_valid_sig:
        return MandateVerificationResponse(
            mandate_id=mandate.mandate_id,
            valid=False,
            status=mandate.status,
            is_expired=is_expired,
            is_tampered=True,
            reason=(
                "Cryptographic signature mismatch: permissions or mandate "
                "metadata have been altered."
            ),
        )

    if is_expired:
        return MandateVerificationResponse(
            mandate_id=mandate.mandate_id,
            valid=False,
            status=MandateStatus.EXPIRED,
            is_expired=True,
            is_tampered=False,
            reason="Mandate validity window has expired.",
        )

    if mandate.status == MandateStatus.REVOKED:
        return MandateVerificationResponse(
            mandate_id=mandate.mandate_id,
            valid=False,
            status=MandateStatus.REVOKED,
            is_expired=False,
            is_tampered=False,
            reason="Mandate was explicitly revoked by the user.",
        )

    if mandate.status != MandateStatus.ACTIVE:
        return MandateVerificationResponse(
            mandate_id=mandate.mandate_id,
            valid=False,
            status=mandate.status,
            is_expired=False,
            is_tampered=False,
            reason=(
                f"Mandate is in '{mandate.status.value}' state "
                "(must be ACTIVE to authorize purchases)."
            ),
        )

    return MandateVerificationResponse(
        mandate_id=mandate.mandate_id,
        valid=True,
        status=mandate.status,
        is_expired=False,
        is_tampered=False,
        reason=None,
    )


async def list_mandates(limit: int = 50) -> list[Mandate]:
    """List recent mandates, applying expiration where the validity window has closed."""
    mandates = await list_mandates_from_db(limit=limit)
    expired: list[Mandate] = []
    for mandate in mandates:
        expired.append(await _expire_if_needed(mandate))
    return expired
