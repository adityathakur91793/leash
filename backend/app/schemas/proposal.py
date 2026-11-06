"""Pydantic schemas for LEASH purchase proposals.

A proposal is a purchase *request* from an agent. It is not an authorization
and is not a payment instruction.
"""

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.security import (
    MONEY_QUANTUM,
    canonicalize_proposal_payload,
    format_money,
    proposal_content_hash,
    proposal_fingerprint,
    sanitize_untrusted_text,
)
from app.schemas.mandate import MERCHANT_IDENTIFIER_REGEX, SUPPORTED_CURRENCIES

MAX_QUANTITY = 10_000
MAX_PRODUCT_ID_LENGTH = 128
MAX_PRODUCT_NAME_LENGTH = 256
MAX_CATEGORY_LENGTH = 64
MAX_PRODUCT_SOURCE_LENGTH = 2048
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MIN_IDEMPOTENCY_KEY_LENGTH = 8


class ProposalStatus(str, Enum):
    """Lifecycle states of a purchase proposal.

    Evaluation and approval are recorded as status only. They do not execute
    payment and do not alter proposal commercial terms.
    """

    SUBMITTED = "SUBMITTED"
    EVALUATED = "EVALUATED"
    APPROVED = "APPROVED"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


CONTENT_LOCKED_STATUSES = {
    ProposalStatus.EVALUATED,
    ProposalStatus.APPROVED,
    ProposalStatus.SETTLED,
    ProposalStatus.REJECTED,
    ProposalStatus.WITHDRAWN,
}


def _validate_money(value: Decimal, *, field_name: str, allow_zero: bool) -> Decimal:
    """Reject negative, non-finite, or over-precise monetary values."""
    if value.as_tuple().exponent < -2:
        raise ValueError(f"{field_name} cannot have more than 2 decimal places.")
    if allow_zero:
        if value < Decimal("0"):
            raise ValueError(f"{field_name} cannot be negative.")
    elif value <= Decimal("0"):
        raise ValueError(f"{field_name} must be strictly positive (> 0).")
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


class ProposalCreate(BaseModel):
    """Client payload for submitting a purchase proposal.

    Subtotal and total are omitted: the server recalculates them. Extra fields
    are forbidden so a client cannot smuggle authorization or payment data.
    """

    model_config = ConfigDict(extra="forbid")

    mandate_id: str = Field(..., description="Exactly one associated mandate ID.")
    product_id: str = Field(..., description="Merchant product identifier.")
    product_name: str = Field(..., description="Untrusted product title from the merchant.")
    merchant: str = Field(..., description="Canonical merchant identifier or domain.")
    category: str = Field(..., description="Product category tag.")
    quantity: int = Field(..., ge=1, le=MAX_QUANTITY)
    unit_price: Decimal = Field(..., description="Price per unit before tax and fees.")
    currency: str = Field(..., description="ISO 4217 currency code.")
    tax: Decimal = Field(default=Decimal("0.00"), description="Tax amount.")
    shipping: Decimal = Field(default=Decimal("0.00"), description="Shipping amount.")
    other_charges: Decimal = Field(
        default=Decimal("0.00"),
        description="Any additional charges besides tax and shipping.",
    )
    product_source: str = Field(
        ...,
        description="Untrusted product URL or catalog reference. Never fetched or executed.",
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Optional client key for duplicate submission protection.",
    )

    @field_validator("mandate_id", mode="after")
    @classmethod
    def validate_mandate_id(cls, v: str) -> str:
        """Require a non-empty mandate association."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("mandate_id cannot be empty.")
        return cleaned

    @field_validator("product_id", mode="after")
    @classmethod
    def validate_product_id(cls, v: str) -> str:
        """Treat product IDs as opaque untrusted identifiers."""
        return sanitize_untrusted_text(
            v, field_name="product_id", max_length=MAX_PRODUCT_ID_LENGTH
        )

    @field_validator("product_name", mode="after")
    @classmethod
    def validate_product_name(cls, v: str) -> str:
        """Store product names as opaque text; never execute embedded instructions."""
        return sanitize_untrusted_text(
            v, field_name="product_name", max_length=MAX_PRODUCT_NAME_LENGTH
        )

    @field_validator("merchant", mode="after")
    @classmethod
    def validate_merchant(cls, v: str) -> str:
        """Normalize merchant identity to the same canonical form as mandates."""
        if not isinstance(v, str):
            raise ValueError("merchant must be a string.")
        merchant = v.strip().lower()
        if not merchant:
            raise ValueError("merchant cannot be empty.")
        if merchant.startswith("http://") or merchant.startswith("https://") or "/" in merchant:
            raise ValueError(
                f"Malformed merchant identifier '{v}'. Must not include URI scheme or paths."
            )
        if " " in merchant or not MERCHANT_IDENTIFIER_REGEX.match(merchant):
            raise ValueError(
                f"Malformed merchant identifier '{v}'. Must be a valid domain or identifier."
            )
        return merchant

    @field_validator("category", mode="after")
    @classmethod
    def validate_category(cls, v: str) -> str:
        """Normalize category tags to lowercase opaque labels."""
        return sanitize_untrusted_text(
            v, field_name="category", max_length=MAX_CATEGORY_LENGTH
        ).lower()

    @field_validator("currency", mode="after")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        """Require a supported uppercase ISO 4217 currency."""
        if not isinstance(v, str):
            raise ValueError("currency must be a string.")
        if not v.isupper():
            raise ValueError(
                f"Invalid currency '{v}'. Must be strictly uppercase ISO 4217 code."
            )
        val = v.strip()
        if len(val) != 3 or val not in SUPPORTED_CURRENCIES:
            allowed = ", ".join(sorted(SUPPORTED_CURRENCIES))
            raise ValueError(f"Invalid currency '{v}'. Must be one of: {allowed}.")
        return val

    @field_validator("unit_price", mode="after")
    @classmethod
    def validate_unit_price(cls, v: Decimal) -> Decimal:
        """Unit price must be strictly positive with at most two decimals."""
        return _validate_money(v, field_name="unit_price", allow_zero=False)

    @field_validator("tax", mode="after")
    @classmethod
    def validate_tax(cls, v: Decimal) -> Decimal:
        """Tax may be zero but cannot be negative."""
        return _validate_money(v, field_name="tax", allow_zero=True)

    @field_validator("shipping", mode="after")
    @classmethod
    def validate_shipping(cls, v: Decimal) -> Decimal:
        """Shipping may be zero but cannot be negative."""
        return _validate_money(v, field_name="shipping", allow_zero=True)

    @field_validator("other_charges", mode="after")
    @classmethod
    def validate_other_charges(cls, v: Decimal) -> Decimal:
        """Other charges may be zero but cannot be negative."""
        return _validate_money(v, field_name="other_charges", allow_zero=True)

    @field_validator("product_source", mode="after")
    @classmethod
    def validate_product_source(cls, v: str) -> str:
        """Treat product sources as untrusted references; never fetch or execute."""
        return sanitize_untrusted_text(
            v, field_name="product_source", max_length=MAX_PRODUCT_SOURCE_LENGTH
        )

    @field_validator("idempotency_key", mode="after")
    @classmethod
    def validate_idempotency_key(cls, v: str | None) -> str | None:
        """Restrict optional idempotency keys to a conservative token alphabet."""
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            return None
        if len(cleaned) < MIN_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError(
                f"idempotency_key must be at least {MIN_IDEMPOTENCY_KEY_LENGTH} characters."
            )
        if len(cleaned) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError(
                f"idempotency_key exceeds maximum length of {MAX_IDEMPOTENCY_KEY_LENGTH}."
            )
        if not all(ch.isalnum() or ch in "._:-" for ch in cleaned):
            raise ValueError("idempotency_key contains unsupported characters.")
        return cleaned

    def computed_subtotal(self) -> Decimal:
        """Server-side subtotal: quantity × unit price."""
        raw = self.unit_price * Decimal(self.quantity)
        return raw.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

    def computed_total(self) -> Decimal:
        """Server-side total: subtotal + tax + shipping + other charges."""
        raw = self.computed_subtotal() + self.tax + self.shipping + self.other_charges
        return raw.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


class Proposal(ProposalCreate):
    """Persisted purchase proposal with server-calculated totals and integrity hash."""

    proposal_id: str
    schema_version: str = "1.0"
    status: ProposalStatus = ProposalStatus.SUBMITTED
    subtotal: Decimal
    total: Decimal
    created_at: datetime
    fingerprint: str
    content_hash: str

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_created_tz(cls, v: datetime) -> datetime:
        """Ensure creation timestamp is timezone-aware UTC."""
        if v.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware UTC datetime.")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_server_totals(self) -> "Proposal":
        """Reject records whose totals do not match server arithmetic."""
        expected_subtotal = self.computed_subtotal()
        expected_total = self.computed_total()
        if self.subtotal != expected_subtotal:
            raise ValueError(
                "subtotal does not match server-calculated quantity * unit_price "
                f"({format_money(expected_subtotal)})."
            )
        if self.total != expected_total:
            raise ValueError(
                "total does not match server-calculated "
                "subtotal + tax + shipping + other_charges "
                f"({format_money(expected_total)})."
            )
        return self

    def canonical_payload(self) -> str:
        """Deterministic canonical JSON of immutable proposal terms."""
        return canonicalize_proposal_payload(self.model_dump())

    def computed_fingerprint(self) -> str:
        """Recompute the duplicate-protection fingerprint."""
        return proposal_fingerprint(self.model_dump())

    def computed_content_hash(self) -> str:
        """Recompute the integrity hash from canonical terms."""
        return proposal_content_hash(self.model_dump())


def build_proposal_from_create(
    payload: ProposalCreate,
    *,
    proposal_id: str,
    created_at: datetime,
    status: ProposalStatus = ProposalStatus.SUBMITTED,
) -> Proposal:
    """Construct a Proposal with server-calculated totals and integrity hashes."""
    data: dict[str, Any] = payload.model_dump()
    data.update(
        {
            "proposal_id": proposal_id,
            "schema_version": "1.0",
            "status": status,
            "subtotal": payload.computed_subtotal(),
            "total": payload.computed_total(),
            "created_at": created_at,
            "fingerprint": "",
            "content_hash": "",
        }
    )
    data["fingerprint"] = proposal_fingerprint(data)
    data["content_hash"] = proposal_content_hash(data)
    return Proposal.model_validate(data)
