"""Tests for HMAC-SHA256 mandate signing, verification, and canonical JSON."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.security import (
    canonicalize_mandate_payload,
    sign_payload,
    validate_signing_secret,
    verify_signature,
)
from app.schemas.mandate import Mandate, MandateStatus

VALID_SECRET = "leash-test-secret-signing-key-32-bytes-minimum!!"
WEAK_SECRET = "too-short-secret"


def _create_test_mandate() -> Mandate:
    return Mandate(
        mandate_id="man_test_123",
        schema_version="1.0",
        status=MandateStatus.CONFIRMED,
        max_total_budget=Decimal("40.00"),
        currency="USD",
        allowed_merchants=["target.com", "amazon.com"],
        allowed_categories=["books", "gifts"],
        max_item_count=2,
        max_quantity_per_item=1,
        max_price_per_item=Decimal("25.00"),
        allow_subscriptions=False,
        require_user_approval=True,
        created_at=datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc),
    )


def test_deterministic_serialization() -> None:
    """Test canonical JSON is identical regardless of input ordering."""
    mandate_1 = _create_test_mandate()
    dict_1 = mandate_1.model_dump()

    # Reordered merchants and categories in raw dict
    dict_2 = dict_1.copy()
    dict_2["allowed_merchants"] = ["amazon.com", "target.com"]
    dict_2["allowed_categories"] = ["gifts", "books"]

    canonical_1 = canonicalize_mandate_payload(dict_1)
    canonical_2 = canonicalize_mandate_payload(dict_2)

    assert canonical_1 == canonical_2

    # Verify JSON structure formatting: sorted keys, compact separators
    parsed = json.loads(canonical_1)
    assert parsed["max_total_budget"] == "40.00"
    assert parsed["max_price_per_item"] == "25.00"
    assert parsed["allowed_merchants"] == ["amazon.com", "target.com"]
    assert parsed["allowed_categories"] == ["books", "gifts"]
    assert " " not in canonical_1.replace('": "', "")  # no whitespace separators
    assert list(parsed.keys()) == sorted(parsed.keys())
    assert "status" not in parsed
    assert "signature" not in parsed
    assert "signed_at" not in parsed


def test_successful_signing_and_verification() -> None:
    """Test generating a signature and verifying it against the canonical payload."""
    mandate = _create_test_mandate()
    payload = mandate.canonical_payload()

    signature = sign_payload(payload, VALID_SECRET)
    assert isinstance(signature, str)
    assert len(signature) == 64  # SHA256 hex digest length

    # Verification must succeed
    assert verify_signature(payload, signature, VALID_SECRET) is True


def test_tampered_mandate_budget_rejected() -> None:
    """Test that altering the budget invalidates the signature."""
    mandate = _create_test_mandate()
    signature = sign_payload(mandate.canonical_payload(), VALID_SECRET)

    # Tamper with budget
    tampered_data = mandate.model_dump()
    tampered_data["max_total_budget"] = Decimal("50.00")
    tampered_payload = canonicalize_mandate_payload(tampered_data)

    assert verify_signature(tampered_payload, signature, VALID_SECRET) is False


def test_tampered_mandate_merchants_rejected() -> None:
    """Test that injecting an unauthorized merchant invalidates the signature."""
    mandate = _create_test_mandate()
    signature = sign_payload(mandate.canonical_payload(), VALID_SECRET)

    tampered_data = mandate.model_dump()
    tampered_data["allowed_merchants"].append("unauthorized-store.com")
    tampered_payload = canonicalize_mandate_payload(tampered_data)

    assert verify_signature(tampered_payload, signature, VALID_SECRET) is False


def test_tampered_mandate_expiration_rejected() -> None:
    """Test that extending the expiration window invalidates the signature."""
    mandate = _create_test_mandate()
    signature = sign_payload(mandate.canonical_payload(), VALID_SECRET)

    tampered_data = mandate.model_dump()
    tampered_data["expires_at"] = datetime(2028, 1, 1, 0, 0, tzinfo=timezone.utc)
    tampered_payload = canonicalize_mandate_payload(tampered_data)

    assert verify_signature(tampered_payload, signature, VALID_SECRET) is False


def test_invalid_signature_rejection() -> None:
    """Test that malformed or forged signatures are rejected."""
    mandate = _create_test_mandate()
    payload = mandate.canonical_payload()

    assert verify_signature(payload, "invalid_hex_signature", VALID_SECRET) is False
    assert verify_signature(payload, "0" * 64, VALID_SECRET) is False
    assert verify_signature(payload, "", VALID_SECRET) is False
    assert verify_signature(payload, None, VALID_SECRET) is False


def test_missing_or_weak_signing_secret_rejection() -> None:
    """Test that missing or sub-32-byte signing secrets are rejected."""
    mandate = _create_test_mandate()
    payload = mandate.canonical_payload()

    with pytest.raises(ValueError) as exc_info:
        validate_signing_secret("")
    assert "non-empty" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        validate_signing_secret(WEAK_SECRET)
    assert "too weak" in str(exc_info.value)

    with pytest.raises(ValueError):
        sign_payload(payload, WEAK_SECRET)

    with pytest.raises(ValueError):
        verify_signature(payload, "sig", WEAK_SECRET)


def test_permission_modification_after_signing_rejected() -> None:
    """Test that modifying permissions after signing requires generating a new mandate."""
    mandate = _create_test_mandate()
    signature = sign_payload(mandate.canonical_payload(), VALID_SECRET)
    mandate.signature = signature
    mandate.status = MandateStatus.SIGNED

    # Attempting to verify with modified permissions must fail
    modified_data = mandate.model_dump()
    modified_data["max_quantity_per_item"] = 2
    modified_payload = canonicalize_mandate_payload(modified_data)

    assert verify_signature(modified_payload, mandate.signature, VALID_SECRET) is False
