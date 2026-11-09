"""Owner-scoped, minimized activity and audit overview schemas."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ActivityEvent(BaseModel):
    """A safe display projection of one immutable audit event."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    event_type: Literal[
        "MANDATE_CREATED", "MANDATE_CONFIRMED", "MANDATE_SIGNED", "MANDATE_ACTIVATED",
        "MANDATE_REVOKED", "MANDATE_EXPIRED", "PROPOSAL_CREATED", "PROPOSAL_EVALUATED",
        "PROPOSAL_APPROVED", "PURCHASE_CONSENT_RECORDED", "PROPOSAL_REJECTED",
        "EXCEPTION_APPROVED", "REPLAY_ATTEMPT_BLOCKED", "AUTHORIZATION_BLOCKED",
        "PAYMENT_ORDER_CREATED", "PAYMENT_ORDER_ATTEMPT_PREPARED",
        "PAYMENT_ORDER_ATTEMPT_UNKNOWN", "PAYMENT_ORDER_ATTEMPT_RECONCILED",
        "PAYMENT_APPROVAL_OBSERVED", "PAYMENT_CAPTURE_ATTEMPTED", "PAYMENT_CAPTURED",
        "PAYMENT_FAILED", "PAYMENT_RECONCILED", "INTENT_ASSESSMENT_COMPLETED",
        "INTENT_ASSESSMENT_FAILED",
    ]
    entity_type: Literal["MANDATE", "PROPOSAL", "PAYMENT"]
    actor: Literal["USER", "AGENT", "SYSTEM", "ADMIN", "POLICY_ENGINE"]
    summary: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=256)
    amount: str | None = Field(default=None, pattern=r"^(?:0|[1-9]\d*)(?:\.\d{1,2})?$")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class SettlementTotal(BaseModel):
    """Exact sum of locally confirmed completed payments by currency."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    amount: str = Field(..., pattern=r"^(?:0|[1-9]\d*)\.\d{2}$")


class MandateStatusCounts(BaseModel):
    """Exact mandate lifecycle counts; unknown states are rejected."""

    model_config = ConfigDict(extra="forbid")

    DRAFT: int = Field(..., ge=0)
    CONFIRMED: int = Field(..., ge=0)
    SIGNED: int = Field(..., ge=0)
    ACTIVE: int = Field(..., ge=0)
    EXPIRED: int = Field(..., ge=0)
    REVOKED: int = Field(..., ge=0)


class ProposalStatusCounts(BaseModel):
    """Exact proposal lifecycle counts; unknown states are rejected."""

    model_config = ConfigDict(extra="forbid")

    SUBMITTED: int = Field(..., ge=0)
    EVALUATED: int = Field(..., ge=0)
    APPROVED: int = Field(..., ge=0)
    SETTLED: int = Field(..., ge=0)
    REJECTED: int = Field(..., ge=0)
    WITHDRAWN: int = Field(..., ge=0)


class PaymentStatusCounts(BaseModel):
    """Exact local checkout status counts; unknown states are rejected."""

    model_config = ConfigDict(extra="forbid")

    CREATED: int = Field(..., ge=0)
    PAYER_ACTION_REQUIRED: int = Field(..., ge=0)
    APPROVED: int = Field(..., ge=0)
    CAPTURING: int = Field(..., ge=0)
    COMPLETED: int = Field(..., ge=0)
    FAILED: int = Field(..., ge=0)
    CANCELLED: int = Field(..., ge=0)
    RECONCILIATION_REQUIRED: int = Field(..., ge=0)


class UnresolvedCheckoutAttemptCounts(BaseModel):
    """Orphaned attempt states that have no local PayPal order to display."""

    model_config = ConfigDict(extra="forbid")

    UNKNOWN: int = Field(..., ge=0)
    PROVIDER_CREATED: int = Field(..., ge=0)
    RECONCILIATION_REQUIRED: int = Field(..., ge=0)


class ActivityOverviewResponse(BaseModel):
    """Authenticated principal's recent activity and current payment summary."""

    model_config = ConfigDict(extra="forbid")

    items: list[ActivityEvent]
    total: int = Field(..., ge=0)
    has_more: bool
    mandate_status_counts: MandateStatusCounts
    proposal_status_counts: ProposalStatusCounts
    payment_status_counts: PaymentStatusCounts
    unresolved_checkout_attempt_count: int = Field(..., ge=0)
    unresolved_checkout_attempt_status_counts: UnresolvedCheckoutAttemptCounts
    confirmed_settlement_count: int = Field(..., ge=0)
    confirmed_settlement_totals: list[SettlementTotal]
    hash_chain_valid: bool
    integrity_note: str
