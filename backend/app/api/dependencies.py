"""FastAPI authentication dependencies for LEASH.

Authentication design:
- Bearer tokens are transmitted in the ``Authorization: Bearer <token>`` HTTP header.
- Tokens are NOT stored in cookies, so CSRF protection is not required.
  (Cookies are automatically sent by browsers; bearer tokens must be explicitly
   attached by the client, eliminating the CSRF threat model.)
- Clients MUST protect raw bearer tokens as credentials — treat them like passwords.
- Identity is NEVER read from the request body. The session dependency is the
  sole authoritative source of principal identity.
"""

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.schemas.session import UserSession
from app.services.session_service import (
    SessionExpiredError,
    SessionNotFoundError,
    SessionRevokedError,
    validate_session,
)

logger = logging.getLogger(__name__)

# auto_error=False so we can return a descriptive 401 instead of FastAPI's default
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> UserSession:
    """Validate the Bearer token and return the active UserSession.

    Identity is NEVER read from the request body — it is derived exclusively
    from the cryptographically validated bearer token.

    Args:
        credentials: Injected by FastAPI from the Authorization header.

    Returns:
        The validated UserSession for the authenticated principal.

    Raises:
        HTTPException 401: For missing, invalid, expired, or revoked tokens.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide a valid Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await validate_session(credentials.credentials)
    except (SessionNotFoundError, SessionExpiredError, SessionRevokedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
