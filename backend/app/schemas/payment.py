"""Pydantic schemas for Phase 3 PayPal Sandbox Executor."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field


class PayPalOrderStatus(str, Enum):
    """Lifecycle states of a PayPal checkout order in LEASH."""

    CREATED = "CREATED"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class PaymentOrderCreateRequest(BaseModel):
    """Request payload for establishing a PayPal checkout order from an approved proposal."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="ID of the policy-authorized proposal to execute payment for.",
    )
    return_url: str | None = Field(
        default=None,
        description="Optional client redirect URL after user approval on PayPal.",
    )
    cancel_url: str | None = Field(
        default=None,
        description="Optional client redirect URL if user cancels approval on PayPal.",
    )


class PaymentOrderResponse(BaseModel):
    """Response returned when a PayPal checkout order has been created."""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., description="PayPal Order ID (e.g. 5O190127TN364715T).")
    proposal_id: str
    mandate_id: str
    owner_user_id: str
    status: PayPalOrderStatus
    currency: str
    total_amount: Decimal
    approval_url: str = Field(
        ...,
        description="Safe PayPal Sandbox approval URL for human user authentication/approval.",
    )
    created_at: datetime


class PaymentCaptureRequest(BaseModel):
    """Request payload for capturing an approved PayPal order."""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., min_length=1, max_length=200, description="PayPal Order ID.")


class PaymentCaptureResponse(BaseModel):
    """Response returned upon successful payment capture from PayPal."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    proposal_id: str
    mandate_id: str
    capture_id: str = Field(..., description="PayPal Capture ID.")
    status: PayPalOrderStatus
    currency: str
    total_amount: Decimal
    captured_at: datetime
    receipt: dict[str, str] = Field(
        default_factory=dict,
        description="Summary receipt details verifying the executed transaction.",
    )


class PayPalOrderRecord(BaseModel):
    """Internal database representation of a PayPal order in LEASH."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    proposal_id: str
    mandate_id: str
    owner_user_id: str
    status: PayPalOrderStatus
    currency: str
    total_amount: Decimal
    subtotal: Decimal
    tax: Decimal
    shipping: Decimal
    other_charges: Decimal
    approval_url: str
    paypal_request_id: str
    capture_id: str | None = None
    capture_request_id: str | None = None
    created_at: datetime
    updated_at: datetime
    captured_at: datetime | None = None
