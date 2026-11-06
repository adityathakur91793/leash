"""Session lifecycle service for LEASH secure sessions.

Sessions authenticate principals via short-lived bearer tokens. Each session
belongs to exactly one principal (identified by user_id). Multiple sessions
may share the same principal. Session expiry does NOT affect mandate ownership.
"""

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.db.database import (
    create_session_in_db,
    get_connection,  # noqa: F401 (imported for future use by callers)
    get_session_by_hash,
    revoke_session_in_db,
)
from app.schemas.session import (
    SessionCreateRequest,
    SessionInfoResponse,
    SessionResponse,
    UserSession,
)

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 86400  # 24 hours


class SessionNotFoundError(Exception):
    """Raised when a session token does not match any stored session."""


class SessionExpiredError(Exception):
    """Raised when a session's expiration time has passed."""


class SessionRevokedError(Exception):
    """Raised when a session has been explicitly revoked."""


def _generate_session_token() -> str:
    """Generate a 256-bit cryptographically random URL-safe bearer token.

    Returns:
        A 43-character URL-safe base64-encoded string (no padding).
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _hash_session_token(raw_token: str) -> str:
    """SHA-256 hash of the raw bearer token. Only this hash is persisted.

    Args:
        raw_token: The plaintext bearer token returned to the client.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def create_session(
    request: SessionCreateRequest, principal_id: str | None = None
) -> SessionResponse:
    """Create a new session and return the raw bearer token.

    Identity is derived exclusively server-side:
    - If principal_id is provided (from a validated existing session), it is preserved.
    - Otherwise, a cryptographically random internal principal ID is generated with
      prefix 'usr_' (256 bits entropy). Clients cannot choose or supply a principal ID.

    Args:
        request: Validated session creation parameters.
        principal_id: Optional validated principal from an active authenticated session.

    Returns:
        SessionResponse containing the one-time raw bearer token and metadata.
    """
    if not principal_id or not principal_id.strip():
        principal_id = f"usr_{secrets.token_hex(16)}"
    else:
        principal_id = principal_id.strip()

    raw_token = _generate_session_token()
    token_hash = _hash_session_token(raw_token)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=request.ttl_seconds)

    await create_session_in_db(
        token_hash=token_hash,
        user_id=principal_id,
        created_at_iso=now.isoformat(),
        expires_at_iso=expires_at.isoformat(),
    )

    # Intentionally NOT logging token or hash
    logger.info("Session created for principal '%s'", principal_id)

    return SessionResponse(
        session_token=raw_token,
        user_id=principal_id,
        created_at=now.isoformat(),
        expires_at=expires_at.isoformat(),
    )


async def validate_session(raw_token: str) -> UserSession:
    """Validate a bearer token and return the active UserSession.

    Args:
        raw_token: The plaintext bearer token from the Authorization header.

    Returns:
        UserSession for a valid, non-expired, non-revoked session.

    Raises:
        SessionNotFoundError: If no session exists for the given token.
        SessionRevokedError: If the session has been explicitly revoked.
        SessionExpiredError: If the session's TTL has elapsed.
    """
    token_hash = _hash_session_token(raw_token)
    row = await get_session_by_hash(token_hash)
    if row is None:
        raise SessionNotFoundError("Session not found or token invalid.")

    session = UserSession(
        session_token_hash=row["session_token_hash"],
        user_id=row["user_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        revoked_at=(
            datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None
        ),
    )

    if session.revoked_at is not None:
        raise SessionRevokedError("Session has been revoked.")

    now = datetime.now(timezone.utc)
    if now >= session.expires_at:
        raise SessionExpiredError("Session has expired.")

    return session


async def revoke_session(raw_token: str) -> None:
    """Revoke a session by its raw bearer token.

    Args:
        raw_token: The plaintext bearer token to revoke.

    Raises:
        SessionNotFoundError: If no session exists for the given token.
    """
    token_hash = _hash_session_token(raw_token)
    now = datetime.now(timezone.utc)
    revoked = await revoke_session_in_db(
        token_hash=token_hash, revoked_at_iso=now.isoformat()
    )
    if not revoked:
        raise SessionNotFoundError("Session not found.")
    # Log only the hash prefix, never the token or full hash
    logger.info("Session revoked (hash prefix: %s...)", token_hash[:8])


async def get_session_info(raw_token: str) -> SessionInfoResponse:
    """Return public session metadata for a valid session.

    Args:
        raw_token: The plaintext bearer token.

    Returns:
        SessionInfoResponse with public metadata.

    Raises:
        SessionNotFoundError: If token is invalid.
        SessionRevokedError: If session is revoked.
        SessionExpiredError: If session has expired.
    """
    session = await validate_session(raw_token)
    now = datetime.now(timezone.utc)
    return SessionInfoResponse(
        user_id=session.user_id,
        created_at=session.created_at.isoformat(),
        expires_at=session.expires_at.isoformat(),
        is_active=session.revoked_at is None and now < session.expires_at,
    )
