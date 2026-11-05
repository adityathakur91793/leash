"""API endpoints for one-time exception approval tokens.

User-facing (no admin auth required). Allows users to issue and consume
short-lived HMAC-SHA256 approval tokens for proposals requiring user approval.
"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.schemas.approval import (
    ApprovalConsumeRequest,
    ApprovalConsumeResponse,
    ApprovalTokenRequest,
    ApprovalTokenResponse,
)
from app.services.approval_service import consume_approval_token, issue_approval_token
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
) -> ApprovalTokenResponse:
    """Issue a one-time HMAC-SHA256 approval token for a proposal.

    Only available for proposals in REQUIRE_APPROVAL state.
    Hard BLOCK rules (P01, P02, P03, P08, P11, P12) cannot be overridden.
    """
    if body.proposal_id != proposal_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="proposal_id in body must match path parameter.",
        )
    try:
        result = await issue_approval_token(
            proposal_id=proposal_id,
            user_session_id=body.user_session_id,
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
) -> ApprovalConsumeResponse:
    """Consume a one-time approval token to authorize a proposal.

    Verifies HMAC signature, checks expiry, and atomically marks token as used
    to prevent replay attacks. Kill switch must be inactive.
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
            user_session_id=body.user_session_id,
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
