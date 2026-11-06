"""API endpoints for LEASH secure user sessions.

Sessions use Bearer token authentication. Tokens are single-use from the
creation response — they cannot be re-retrieved after the initial response.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies import get_current_session
from app.schemas.session import (
    SessionCreateRequest,
    SessionInfoResponse,
    SessionResponse,
    UserSession,
)
from app.services.session_service import (
    SessionNotFoundError,
    create_session,
    revoke_session,
    validate_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["Sessions"])

# Bearer extractor for optional auth or token extraction
_bearer = HTTPBearer(auto_error=False)


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session_endpoint(
    body: SessionCreateRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> SessionResponse:
    """Create a new authenticated session.

    If an active session token is presented in the Authorization header, the new
    session inherits the authenticated principal identity (multi-session).
    Anonymous requests always receive a server-generated principal ID.
    Clients can NEVER supply or choose an arbitrary principal ID.
    """
    authenticated_principal: str | None = None
    if credentials is not None:
        try:
            active_session = await validate_session(credentials.credentials)
            authenticated_principal = active_session.user_id
        except Exception:
            authenticated_principal = None
    return await create_session(body, principal_id=authenticated_principal)


@router.get("/me", response_model=SessionInfoResponse)
async def get_session_info_endpoint(
    session: UserSession = Depends(get_current_session),
) -> SessionInfoResponse:
    """Return public metadata for the currently authenticated session."""
    return SessionInfoResponse(
        user_id=session.user_id,
        created_at=session.created_at.isoformat(),
        expires_at=session.expires_at.isoformat(),
        is_active=True,  # already validated by dependency
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session_endpoint(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: UserSession = Depends(get_current_session),  # noqa: ARG001
) -> None:
    """Revoke the current session. The token becomes immediately invalid.

    The ``session`` dependency validates the token before revocation so that
    only the token holder can revoke their own session.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        await revoke_session(credentials.credentials)
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
