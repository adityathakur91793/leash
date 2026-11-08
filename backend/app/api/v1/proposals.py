"""API endpoints for purchase proposals.

Creation, retrieval, and listing only. Authorization and payment are out of
scope for this resource.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from app.api.dependencies import get_current_session
from app.schemas.session import UserSession

from app.schemas.policy import PolicyDecision
from app.schemas.intent_audit import ProposalIntentAssessmentResponse
from app.schemas.proposal import Proposal, ProposalCreate
from app.services.mandate_service import MandateNotFoundError
from app.services.mandate_service import get_mandate_from_db
from app.services.proposal_service import (
    CurrencyMismatchError,
    DuplicateProposalConflictError,
    ProposalNotFoundError,
    ProposalTamperedError,
    create_proposal,
    evaluate_proposal_service,
    get_proposal_for_owner,
    list_proposals,
)
from app.services.intent_auditor_client import IntentAuditorUnavailable
from app.services.intent_audit_service import AssessmentRetrievalError, get_owner_assessment

router = APIRouter(prefix="/proposals", tags=["Proposals"])


async def _require_owned_mandate(mandate_id: str, user_id: str) -> None:
    """Reject missing and foreign mandates identically without mutating them."""
    mandate = await get_mandate_from_db(mandate_id)
    if mandate is None or mandate.owner_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Mandate not found.")


async def _owned_proposal_or_404(proposal_id: str, user_id: str) -> Proposal:
    """Return a proposal only when its immutable mandate owner matches the session."""
    try:
        return await get_proposal_for_owner(proposal_id, user_id)
    except ProposalNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Proposal not found.") from None


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
async def create_proposal_endpoint(
    payload: ProposalCreate,
    session: UserSession = Depends(get_current_session),
) -> Proposal | JSONResponse:
    """Submit a purchase proposal associated with exactly one mandate.

    Server recalculates subtotal and total. Duplicate fingerprints or matching
    idempotency keys return the original proposal without modification.
    """
    try:
        await _require_owned_mandate(payload.mandate_id, session.user_id)
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
    session: UserSession = Depends(get_current_session),
) -> list[Proposal]:
    """List recent proposals, optionally filtered by mandate ID."""
    try:
        if mandate_id is not None:
            await _require_owned_mandate(mandate_id, session.user_id)
        return await list_proposals(
            limit=limit, mandate_id=mandate_id, owner_user_id=session.user_id
        )
    except (MandateNotFoundError, ProposalTamperedError) as exc:
        raise _http_for_proposal_errors(exc) from exc


@router.get("/{proposal_id}", response_model=Proposal)
async def get_proposal_endpoint(
    proposal_id: str,
    session: UserSession = Depends(get_current_session),
) -> Proposal:
    """Retrieve a stored proposal after integrity verification."""
    try:
        return await _owned_proposal_or_404(proposal_id, session.user_id)
    except (ProposalNotFoundError, ProposalTamperedError) as exc:
        raise _http_for_proposal_errors(exc) from exc


@router.get(
    "/{proposal_id}/auditor-assessment",
    response_model=ProposalIntentAssessmentResponse,
)
async def get_proposal_auditor_assessment_endpoint(
    proposal_id: str,
    session: UserSession = Depends(get_current_session),
) -> ProposalIntentAssessmentResponse:
    """Read the latest persisted auditor assessment for the proposal owner."""
    try:
        await _owned_proposal_or_404(proposal_id, session.user_id)
        return await get_owner_assessment(proposal_id, session.user_id)
    except (ProposalNotFoundError, MandateNotFoundError, ProposalTamperedError) as exc:
        raise _http_for_proposal_errors(exc) from exc
    except AssessmentRetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Assessment data is unavailable.",
        ) from exc


@router.post("/{proposal_id}/evaluate", response_model=PolicyDecision)
async def evaluate_proposal_endpoint(
    proposal_id: str, session: UserSession = Depends(get_current_session)
) -> PolicyDecision:
    """Evaluate only a proposal owned by the authenticated session principal."""
    try:
        await _owned_proposal_or_404(proposal_id, session.user_id)
        return await evaluate_proposal_service(proposal_id)
    except IntentAuditorUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Intent assessment is unavailable; proposal remains on hold.") from exc
    except (ProposalNotFoundError, MandateNotFoundError, ProposalTamperedError) as exc:
        raise _http_for_proposal_errors(exc) from exc


@router.post("/{proposal_id}/evaluate-review", response_model=PolicyDecision)
async def evaluate_proposal_for_review_endpoint(
    proposal_id: str, session: UserSession = Depends(get_current_session)
) -> PolicyDecision:
    """Evaluate policy and auditor for review without approving or paying."""
    try:
        await _owned_proposal_or_404(proposal_id, session.user_id)
        return await evaluate_proposal_service(proposal_id)
    except IntentAuditorUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Intent assessment is unavailable; proposal remains on hold.",
        ) from exc
    except (ProposalNotFoundError, MandateNotFoundError, ProposalTamperedError) as exc:
        raise _http_for_proposal_errors(exc) from exc
