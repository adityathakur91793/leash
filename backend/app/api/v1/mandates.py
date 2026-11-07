"""API endpoints for managing and verifying purchase mandates."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import get_current_session
from app.schemas.mandate import (
    Mandate,
    MandateCreate,
    MandateVerificationResponse,
)
from app.schemas.session import UserSession
from app.services.mandate_service import (
    InvalidStateTransitionError,
    MandateExpiredError,
    MandateNotFoundError,
    MandateTamperedError,
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    get_mandate,
    get_mandate_from_db,
    list_mandates_for_owner,
    revoke_mandate,
    sign_mandate,
    verify_mandate,
)

router = APIRouter(prefix="/mandates", tags=["Mandates"])


async def _owned_mandate_or_404(mandate_id: str, user_id: str) -> Mandate:
    """Load an owned mandate without revealing whether another owner's exists."""
    stored = await get_mandate_from_db(mandate_id)
    if stored is None or stored.owner_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Mandate not found.")
    try:
        mandate = await get_mandate(mandate_id)
    except MandateNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Mandate not found.") from None
    if mandate.owner_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Mandate not found.")
    return mandate


@router.post("", response_model=Mandate, status_code=status.HTTP_201_CREATED)
async def create_mandate_endpoint(
    payload: MandateCreate,
    session: UserSession = Depends(get_current_session),
) -> Mandate:
    """Create a new purchase authorization mandate in DRAFT status.

    Requires an authenticated session. The session's principal becomes the
    permanent owner of the mandate and is the only principal authorized to
    issue and consume approval tokens for it.
    """
    return await create_draft_mandate(payload, owner_user_id=session.user_id)


@router.get("", response_model=list[Mandate])
async def list_mandates_endpoint(
    limit: int = Query(default=50, ge=1, le=100),
    session: UserSession = Depends(get_current_session),
) -> list[Mandate]:
    """List mandates belonging only to the authenticated principal."""
    return await list_mandates_for_owner(session.user_id, limit=limit)


@router.get("/{mandate_id}", response_model=Mandate)
async def get_mandate_endpoint(
    mandate_id: str,
    session: UserSession = Depends(get_current_session),
) -> Mandate:
    """Retrieve details of an existing mandate."""
    try:
        return await _owned_mandate_or_404(mandate_id, session.user_id)
    except MandateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{mandate_id}/confirm", response_model=Mandate)
async def confirm_mandate_endpoint(
    mandate_id: str,
    session: UserSession = Depends(get_current_session),
) -> Mandate:
    """Confirm user permissions on a DRAFT mandate before signing."""
    try:
        await _owned_mandate_or_404(mandate_id, session.user_id)
        return await confirm_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MandateExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{mandate_id}/sign", response_model=Mandate)
async def sign_mandate_endpoint(
    mandate_id: str,
    session: UserSession = Depends(get_current_session),
) -> Mandate:
    """Cryptographically sign a CONFIRMED mandate using server-side HMAC-SHA256."""
    try:
        await _owned_mandate_or_404(mandate_id, session.user_id)
        return await sign_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MandateExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post("/{mandate_id}/activate", response_model=Mandate)
async def activate_mandate_endpoint(
    mandate_id: str,
    session: UserSession = Depends(get_current_session),
) -> Mandate:
    """Verify signature integrity and activate a SIGNED mandate for purchasing."""
    try:
        await _owned_mandate_or_404(mandate_id, session.user_id)
        return await activate_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MandateExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except MandateTamperedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{mandate_id}/revoke", response_model=Mandate)
async def revoke_mandate_endpoint(
    mandate_id: str,
    session: UserSession = Depends(get_current_session),
) -> Mandate:
    """Permanently revoke a mandate to prevent any further purchasing activity."""
    try:
        await _owned_mandate_or_404(mandate_id, session.user_id)
        return await revoke_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{mandate_id}/verify", response_model=MandateVerificationResponse)
async def verify_mandate_endpoint(
    mandate_id: str,
    session: UserSession = Depends(get_current_session),
) -> MandateVerificationResponse:
    """Verify cryptographic authenticity, validity window, and active status of a mandate."""
    try:
        await _owned_mandate_or_404(mandate_id, session.user_id)
        return await verify_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
