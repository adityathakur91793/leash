"""API endpoints for purchase proposals.

Creation, retrieval, and listing only. Authorization and payment are out of
scope for this resource.
"""

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.schemas.policy import PolicyDecision
from app.schemas.proposal import Proposal, ProposalCreate
from app.services.mandate_service import MandateNotFoundError
from app.services.proposal_service import (
    CurrencyMismatchError,
    DuplicateProposalConflictError,
    ProposalNotFoundError,
    ProposalTamperedError,
    create_proposal,
    evaluate_proposal_service,
    get_proposal,
    list_proposals,
)

router = APIRouter(prefix="/proposals", tags=["Proposals"])


def _http_for_proposal_errors(exc: Exception) -> HTTPException:
    """Map proposal domain errors to HTTP errors without leaking internals."""
    if isinstance(exc, MandateNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ProposalNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ProposalTamperedError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    if isinstance(exc, DuplicateProposalConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, CurrencyMismatchError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unexpected proposal error.",
    )


@router.post("", response_model=Proposal, status_code=status.HTTP_201_CREATED)
async def create_proposal_endpoint(payload: ProposalCreate) -> Proposal | JSONResponse:
    """Submit a purchase proposal associated with exactly one mandate.

    Server recalculates subtotal and total. Duplicate fingerprints or matching
    idempotency keys return the original proposal without modification.
    """
    try:
        proposal, created = await create_proposal(payload)
    except (
        MandateNotFoundError,
        DuplicateProposalConflictError,
        CurrencyMismatchError,
    ) as exc:
        raise _http_for_proposal_errors(exc) from exc

    if created:
        return proposal
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=proposal.model_dump(mode="json"),
    )


@router.get("", response_model=list[Proposal])
async def list_proposals_endpoint(
    limit: int = Query(default=50, ge=1, le=100),
    mandate_id: str | None = Query(default=None),
) -> list[Proposal]:
    """List recent proposals, optionally filtered by mandate ID."""
    try:
        return await list_proposals(limit=limit, mandate_id=mandate_id)
    except (MandateNotFoundError, ProposalTamperedError) as exc:
        raise _http_for_proposal_errors(exc) from exc


@router.get("/{proposal_id}", response_model=Proposal)
async def get_proposal_endpoint(proposal_id: str) -> Proposal:
    """Retrieve a stored proposal after integrity verification."""
    try:
        return await get_proposal(proposal_id)
    except (ProposalNotFoundError, ProposalTamperedError) as exc:
        raise _http_for_proposal_errors(exc) from exc


@router.post("/{proposal_id}/evaluate", response_model=PolicyDecision)
async def evaluate_proposal_endpoint(proposal_id: str) -> PolicyDecision:
    """Evaluate a stored purchase proposal against its mandate using the policy engine."""
    try:
        return await evaluate_proposal_service(proposal_id)
    except (ProposalNotFoundError, MandateNotFoundError, ProposalTamperedError) as exc:
        raise _http_for_proposal_errors(exc) from exc
