"""Pydantic schemas for server-issued user sessions."""

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class UserSession(BaseModel):
    """Authenticated user session model representing database state."""

    model_config = ConfigDict(extra="forbid")

    session_token_hash: str = Field(
        ...,
        description="SHA-256 hash of the server-issued session token.",
    )
    user_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Identifier of the authenticated user.",
    )
    created_at: datetime = Field(
        ...,
        description="UTC creation timestamp of the session.",
    )
    expires_at: datetime = Field(
        ...,
        description="UTC expiration timestamp of the session.",
    )
    revoked_at: datetime | None = Field(
        default=None,
        description="UTC revocation timestamp of the session if revoked.",
    )


class SessionCreateRequest(BaseModel):
    """Request payload for establishing a new authenticated user session.

    Anonymous clients receive a server-generated principal ID. Clients cannot
    choose or supply a user_id; identity is derived exclusively server-side.
    """

    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(
        default=86400,
        ge=60,
        le=2592000,
        description="Lifetime of the session in seconds (default 24h, max 30d).",
    )


class SessionResponse(BaseModel):
    """Response returned upon session creation containing raw bearer token."""

    model_config = ConfigDict(extra="forbid")

    session_token: str = Field(
        ...,
        description="High-entropy raw bearer token. Transmit securely; stored hashed.",
    )
    user_id: str = Field(
        ...,
        description="Identifier of the authenticated user.",
    )
    created_at: str = Field(
        ...,
        description="ISO-8601 UTC creation timestamp.",
    )
    expires_at: str = Field(
        ...,
        description="ISO-8601 UTC expiration timestamp.",
    )


class SessionInfoResponse(BaseModel):
    """Public metadata for an active session."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    created_at: str
    expires_at: str
    is_active: bool
