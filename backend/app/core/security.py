"""Security utilities — authentication, authorization, and cryptographic helpers."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

MIN_SIGNING_KEY_BYTES = 32


def sanitize_input(value: str) -> str:
    """Strip leading and trailing whitespace from *value*."""
    return value.strip()


def validate_signing_secret(secret: str | None) -> None:
    """Validate that the signing secret is present and meets minimum strength requirements."""
    if not secret or not isinstance(secret, str):
        raise ValueError("Mandate signing secret must be a non-empty string.")
    if len(secret.encode("utf-8")) < MIN_SIGNING_KEY_BYTES:
        raise ValueError(
            f"Mandate signing secret is too weak (must be at least {MIN_SIGNING_KEY_BYTES} bytes)."
        )


def sign_payload(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature for a canonical payload."""
    validate_signing_secret(secret)
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(payload: str, signature: str | None, secret: str) -> bool:
    """Verify HMAC-SHA256 signature using constant-time comparison."""
    validate_signing_secret(secret)
    if not signature or not isinstance(signature, str):
        return False
    expected = sign_payload(payload, secret)
    try:
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError):
        return False


def canonicalize_mandate_payload(data: dict[str, Any]) -> str:
    """Serialize mandate permissions into a deterministic, sorted canonical JSON string.

    Ensures that Decimal amounts are formatted with fixed 2 decimal places,
    datetimes are normalized to ISO 8601 UTC strings, lists are deduplicated
    and sorted, and dictionary keys are sorted with compact separators.
    """
    mandate_id = str(data["mandate_id"]).strip()
    schema_version = str(data.get("schema_version", "1.0")).strip()
    currency = str(data["currency"]).strip().upper()

    # Format Decimal deterministically to 2 decimal places
    raw_budget = data["max_total_budget"]
    budget_str = f"{Decimal(str(raw_budget)):.2f}"

    raw_item_price = data.get("max_price_per_item")
    item_price_str = (
        f"{Decimal(str(raw_item_price)):.2f}"
        if raw_item_price is not None
        else None
    )

    # Format datetimes deterministically to ISO 8601 UTC
    created_at = data["created_at"]
    if isinstance(created_at, str):
        created_str = datetime.fromisoformat(created_at).astimezone(timezone.utc).isoformat()
    elif isinstance(created_at, datetime):
        created_str = created_at.astimezone(timezone.utc).isoformat()
    else:
        raise ValueError("created_at must be an ISO string or datetime object.")

    expires_at = data["expires_at"]
    if isinstance(expires_at, str):
        expires_str = datetime.fromisoformat(expires_at).astimezone(timezone.utc).isoformat()
    elif isinstance(expires_at, datetime):
        expires_str = expires_at.astimezone(timezone.utc).isoformat()
    else:
        raise ValueError("expires_at must be an ISO string or datetime object.")

    # Canonicalize and sort lists
    raw_merchants = data["allowed_merchants"]
    allowed_merchants = sorted(list({str(m).strip().lower() for m in raw_merchants}))

    raw_categories = data["allowed_categories"]
    allowed_categories = sorted(list({str(c).strip().lower() for c in raw_categories}))

    canonical_dict = {
        "allow_subscriptions": bool(data["allow_subscriptions"]),
        "allowed_categories": allowed_categories,
        "allowed_merchants": allowed_merchants,
        "created_at": created_str,
        "currency": currency,
        "expires_at": expires_str,
        "mandate_id": mandate_id,
        "max_item_count": int(data["max_item_count"]),
        "max_price_per_item": item_price_str,
        "max_quantity_per_item": int(data["max_quantity_per_item"]),
        "max_total_budget": budget_str,
        "require_user_approval": bool(data["require_user_approval"]),
        "schema_version": schema_version,
    }

    return json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
