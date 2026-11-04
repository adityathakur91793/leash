"""Pydantic schemas and models for the LEASH Mandate System."""

import re
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.security import canonicalize_mandate_payload

SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD", "JPY"}
MERCHANT_IDENTIFIER_REGEX = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


class MandateStatus(str, Enum):
    """Lifecycle states of a mandate."""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    SIGNED = "SIGNED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class MandateBase(BaseModel):
    """Shared mandate attributes and validation logic."""

    model_config = ConfigDict(extra="forbid")

    max_total_budget: Decimal = Field(
        ...,
        description="Maximum total spending limit for the mandate.",
    )
    currency: str = Field(
        default="USD",
        description="Three-letter ISO 4217 currency code.",
    )
    allowed_merchants: list[str] = Field(
        ...,
        description="Explicit list of permitted merchant domains or identifiers.",
    )
    allowed_categories: list[str] = Field(
        ...,
        description="Explicit list of permitted product/service categories.",
    )
    max_item_count: int = Field(
        default=1,
        ge=1,
        description="Maximum total number of items allowed to be purchased.",
    )
    max_quantity_per_item: int = Field(
        default=1,
        ge=1,
        description="Maximum quantity allowed for any single item.",
    )
    max_price_per_item: Decimal | None = Field(
        default=None,
        description="Optional spending limit for any individual item.",
    )
    allow_subscriptions: bool = Field(
        default=False,
        description="Whether recurring or subscription purchases are permitted.",
    )
    require_user_approval: bool = Field(
        default=True,
        description="Whether individual purchase transactions require explicit user sign-off.",
    )
    expires_at: datetime = Field(
        ...,
        description="Timezone-aware UTC expiration timestamp.",
    )

    @field_validator("currency", mode="after")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        """Validate currency is an approved 3-letter uppercase ISO 4217 code."""
        if not isinstance(v, str):
            raise ValueError("currency must be a string.")
        if not v.isupper():
            raise ValueError(f"Invalid currency '{v}'. Must be strictly uppercase ISO 4217 code.")
        val = v.strip()
        if len(val) != 3 or val not in SUPPORTED_CURRENCIES:
            allowed = ", ".join(sorted(SUPPORTED_CURRENCIES))
            raise ValueError(f"Invalid currency '{v}'. Must be one of: {allowed}.")
        return val

    @field_validator("max_total_budget", mode="after")
    @classmethod
    def validate_budget(cls, v: Decimal) -> Decimal:
        """Validate that the budget is strictly positive and has at most 2 decimal places."""
        if v <= Decimal("0"):
            raise ValueError("max_total_budget must be strictly positive (> 0).")
        if v.as_tuple().exponent < -2:
            raise ValueError("max_total_budget cannot have more than 2 decimal places.")
        return v

    @field_validator("max_price_per_item", mode="after")
    @classmethod
    def validate_item_price(cls, v: Decimal | None) -> Decimal | None:
        """Validate optional per-item price limit."""
        if v is not None:
            if v <= Decimal("0"):
                raise ValueError("max_price_per_item must be strictly positive (> 0).")
            if v.as_tuple().exponent < -2:
                raise ValueError("max_price_per_item cannot have more than 2 decimal places.")
        return v

    @field_validator("allowed_merchants", mode="after")
    @classmethod
    def validate_merchants(cls, v: list[str]) -> list[str]:
        """Validate and sanitize merchant identifiers/domains."""
        if not v:
            raise ValueError("allowed_merchants must contain at least one merchant identifier.")
        cleaned = []
        for m in v:
            if not isinstance(m, str):
                raise ValueError("Merchant identifier must be a string.")
            m_clean = m.strip().lower()
            if not m_clean:
                raise ValueError("Merchant identifier cannot be empty.")
            if m_clean.startswith("http://") or m_clean.startswith("https://") or "/" in m_clean:
                raise ValueError(
                    f"Malformed merchant identifier '{m}'. Must not include URI scheme or paths."
                )
            if " " in m_clean or not MERCHANT_IDENTIFIER_REGEX.match(m_clean):
                raise ValueError(
                    f"Malformed merchant identifier '{m}'. Must be a valid domain or identifier."
                )
            cleaned.append(m_clean)
        return sorted(list(set(cleaned)))

    @field_validator("allowed_categories", mode="after")
    @classmethod
    def validate_categories(cls, v: list[str]) -> list[str]:
        """Validate and sanitize category tags."""
        if not v:
            raise ValueError("allowed_categories must contain at least one category.")
        cleaned = []
        for c in v:
            if not isinstance(c, str):
                raise ValueError("Category must be a string.")
            c_clean = c.strip().lower()
            if not c_clean:
                raise ValueError("Category cannot be empty.")
            cleaned.append(c_clean)
        return sorted(list(set(cleaned)))

    @field_validator("expires_at", mode="after")
    @classmethod
    def validate_timezone(cls, v: datetime) -> datetime:
        """Ensure expiration timestamp is timezone-aware and normalized to UTC."""
        if v.tzinfo is None:
            raise ValueError("expires_at must be a timezone-aware UTC datetime.")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_model_consistency(self) -> "MandateBase":
        """Validate multi-field consistency constraints."""
        if self.max_quantity_per_item > self.max_item_count:
            raise ValueError("max_quantity_per_item cannot exceed max_item_count.")

        if self.max_price_per_item is not None and self.max_price_per_item > self.max_total_budget:
            raise ValueError("max_price_per_item cannot exceed max_total_budget.")

        return self


class MandateCreate(MandateBase):
    """Payload for creating a new draft mandate."""

    @model_validator(mode="after")
    def validate_expiration_in_the_future(self) -> "MandateCreate":
        """Verify expiration is strictly after the current UTC time."""
        created = datetime.now(timezone.utc)
        if self.expires_at <= created:
            raise ValueError("expires_at must be strictly after created_at.")
        return self


class Mandate(MandateBase):
    """Full mandate model with lifecycle status, cryptographic signature, and audit metadata."""

    mandate_id: str = Field(
        ...,
        description="Unique identifier for the mandate.",
    )
    schema_version: str = Field(
        default="1.0",
        description="Mandate schema version.",
    )
    status: MandateStatus = Field(
        default=MandateStatus.DRAFT,
        description="Current lifecycle state of the mandate.",
    )
    created_at: datetime = Field(
        ...,
        description="Mandate creation timestamp in UTC.",
    )
    signature: str | None = Field(
        default=None,
        description="HMAC-SHA256 signature of canonical mandate permissions.",
    )
    signed_at: datetime | None = Field(
        default=None,
        description="Timestamp when the mandate was cryptographically signed.",
    )
    signature_algorithm: str | None = Field(
        default=None,
        description="Cryptographic algorithm used for signing (e.g. HMAC-SHA256).",
    )

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_created_tz(cls, v: datetime) -> datetime:
        """Ensure creation timestamp is timezone-aware UTC."""
        if v.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware UTC datetime.")
        return v.astimezone(timezone.utc)

    @field_validator("signed_at", mode="after")
    @classmethod
    def validate_signed_tz(cls, v: datetime | None) -> datetime | None:
        """Ensure signed_at timestamp is timezone-aware UTC if present."""
        if v is not None:
            if v.tzinfo is None:
                raise ValueError("signed_at must be a timezone-aware UTC datetime.")
            return v.astimezone(timezone.utc)
        return None

    def canonical_payload(self) -> str:
        """Generate the deterministic canonical JSON string for this mandate."""
        return canonicalize_mandate_payload(self.model_dump())


class MandateVerificationResponse(BaseModel):
    """Response returned when validating a mandate's integrity, expiration, and status."""

    model_config = ConfigDict(extra="forbid")

    mandate_id: str
    valid: bool
    status: MandateStatus
    is_expired: bool
    is_tampered: bool
    reason: str | None = None
