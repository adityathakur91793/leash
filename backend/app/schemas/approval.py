"""Pydantic schemas for one-time exception approval tokens."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApprovalTokenRequest(BaseModel):
    """Request body for issuing a one-time approval token.

    The authenticated principal identity is derived from the Bearer token
    in the Authorization header — it is NOT supplied in the request body.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(..., min_length=1, max_length=200)


class ApprovalTokenResponse(BaseModel):
    """Response carrying a newly issued HMAC approval token.

    The token field contains the raw HMAC value. The client must store this
    securely and present it to the consume endpoint within the TTL window.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    issued_at: str  # ISO-8601 UTC timestamp
    expires_at: str  # ISO-8601 UTC timestamp
    nonce: str  # Random hex identifier (not the token)
    token: str  # HMAC-SHA256 hex token — client must store securely


class ApprovalConsumeRequest(BaseModel):
    """Request body for consuming a one-time approval token.

    The authenticated principal identity is derived from the Bearer token
    in the Authorization header — it is NOT supplied in the request body.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(..., min_length=1, max_length=200)
    token: str = Field(..., min_length=1, max_length=200)
    issued_at: str = Field(
        ...,
        description="ISO-8601 UTC timestamp returned when the token was issued.",
    )
    nonce: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Cryptographic nonce returned when the token was issued.",
    )


class ApprovalConsumeResponse(BaseModel):
    """Response indicating whether a one-time approval token was successfully consumed."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    reason: str
    proposal_id: str
