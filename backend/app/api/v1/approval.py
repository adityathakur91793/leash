"""API endpoints for one-time exception approval tokens.

Requires authenticated session via Bearer token. The principal identity is
derived exclusively from the session — it is never read from the request body.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_current_session
from app.schemas.approval import (
    ApprovalConsumeRequest,
    ApprovalConsumeResponse,
    ApprovalTokenRequest,
    ApprovalTokenResponse,
)
from app.schemas.session import UserSession
from app.services.approval_service import (
    MandateOwnershipError,
    OwnerlessMandateError,
    consume_approval_token,
    issue_approval_token,
)
from app.services.mandate_service import MandateNotFoundError
from app.services.proposal_service import ProposalNotFoundError, ProposalTamperedError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals", tags=["Approvals"])


@router.post(
    "/{proposal_id}/approval-token",
    response_model=ApprovalTokenResponse,
    status_code=status.HTTP_200_OK,
)
async def issue_approval_token_endpoint(
    proposal_id: str,
    body: ApprovalTokenRequest,
    session: UserSession = Depends(get_current_session),
) -> ApprovalTokenResponse:
    """Issue a one-time HMAC-SHA256 approval token for a proposal.

    Only available for proposals in REQUIRE_APPROVAL state.
    Hard BLOCK rules (P01, P02, P03, P08, P11, P12) cannot be overridden.
    The authenticated principal must own the mandate the proposal belongs to.
    """
    if body.proposal_id != proposal_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="proposal_id in body must match path parameter.",
        )
    try:
        result = await issue_approval_token(
            proposal_id=proposal_id,
            principal_id=session.user_id,
        )
    except ProposalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except MandateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ProposalTamperedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        # Kill switch active or state unavailable
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except (OwnerlessMandateError, MandateOwnershipError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return ApprovalTokenResponse(**result)


@router.post(
    "/{proposal_id}/approve",
    response_model=ApprovalConsumeResponse,
    status_code=status.HTTP_200_OK,
)
async def consume_approval_token_endpoint(
    proposal_id: str,
    body: ApprovalConsumeRequest,
    session: UserSession = Depends(get_current_session),
) -> ApprovalConsumeResponse:
    """Consume a one-time approval token to authorize a proposal.

    Verifies HMAC signature, checks expiry, and atomically marks token as used
    to prevent replay attacks. Kill switch must be inactive.
    The authenticated principal must own the mandate the proposal belongs to.
    """
    if body.proposal_id != proposal_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="proposal_id in body must match path parameter.",
        )
    try:
        success, reason = await consume_approval_token(
            proposal_id=proposal_id,
            token=body.token,
            principal_id=session.user_id,
            issued_at_iso=body.issued_at,
            nonce=body.nonce,
        )
    except ProposalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except MandateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        # Kill switch active or state unavailable
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except (OwnerlessMandateError, MandateOwnershipError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=reason,
        )

    return ApprovalConsumeResponse(
        success=success,
        reason=reason,
        proposal_id=proposal_id,
    )
