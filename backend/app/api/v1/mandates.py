"""API endpoints for managing and verifying purchase mandates."""

from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.mandate import (
    Mandate,
    MandateCreate,
    MandateVerificationResponse,
)
from app.services.mandate_service import (
    InvalidStateTransitionError,
    MandateExpiredError,
    MandateNotFoundError,
    MandateTamperedError,
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    get_mandate,
    list_mandates,
    revoke_mandate,
    sign_mandate,
    verify_mandate,
)

router = APIRouter(prefix="/mandates", tags=["Mandates"])


@router.post("", response_model=Mandate, status_code=status.HTTP_201_CREATED)
async def create_mandate_endpoint(payload: MandateCreate) -> Mandate:
    """Create a new purchase authorization mandate in DRAFT status."""
    return await create_draft_mandate(payload)


@router.get("", response_model=list[Mandate])
async def list_mandates_endpoint(
    limit: int = Query(default=50, ge=1, le=100)
) -> list[Mandate]:
    """List recent mandates."""
    return await list_mandates(limit=limit)


@router.get("/{mandate_id}", response_model=Mandate)
async def get_mandate_endpoint(mandate_id: str) -> Mandate:
    """Retrieve details of an existing mandate."""
    try:
        return await get_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{mandate_id}/confirm", response_model=Mandate)
async def confirm_mandate_endpoint(mandate_id: str) -> Mandate:
    """Confirm user permissions on a DRAFT mandate before signing."""
    try:
        return await confirm_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MandateExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{mandate_id}/sign", response_model=Mandate)
async def sign_mandate_endpoint(mandate_id: str) -> Mandate:
    """Cryptographically sign a CONFIRMED mandate using server-side HMAC-SHA256."""
    try:
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
async def activate_mandate_endpoint(mandate_id: str) -> Mandate:
    """Verify signature integrity and activate a SIGNED mandate for purchasing."""
    try:
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
async def revoke_mandate_endpoint(mandate_id: str) -> Mandate:
    """Permanently revoke a mandate to prevent any further purchasing activity."""
    try:
        return await revoke_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{mandate_id}/verify", response_model=MandateVerificationResponse)
async def verify_mandate_endpoint(mandate_id: str) -> MandateVerificationResponse:
    """Verify cryptographic authenticity, validity window, and active status of a mandate."""
    try:
        return await verify_mandate(mandate_id)
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
