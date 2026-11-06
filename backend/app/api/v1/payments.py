"""API endpoints for Phase 3 PayPal Sandbox Executor.

Requires authenticated session via Bearer token.
Principal identity is derived exclusively from the session.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_current_session
from app.schemas.payment import (
    PaymentCaptureResponse,
    PaymentOrderCreateRequest,
    PaymentOrderResponse,
)
from app.schemas.session import UserSession
from app.services.approval_service import MandateOwnershipError, OwnerlessMandateError
from app.services.mandate_service import MandateExpiredError, MandateNotFoundError
from app.services.payment_service import (
    PaymentBudgetExceededError,
    PaymentMandateInactiveError,
    PaymentOrderAlreadySettledError,
    PaymentOrderNotApprovedError,
    PaymentOrderNotFoundError,
    PaymentProposalNotApprovedError,
    capture_payment_order,
    create_payment_order,
    get_payment_order,
)
from app.services.paypal_service import (
    PayPalAuthError,
    PayPalCaptureError,
    PayPalConfigError,
    PayPalNetworkError,
    PayPalOrderError,
)
from app.services.proposal_service import CurrencyMismatchError, ProposalNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Payments"])


def _handle_payment_exceptions(exc: Exception) -> HTTPException:
    """Map domain exceptions to corresponding HTTP status codes."""
    if isinstance(exc, (OwnerlessMandateError, MandateOwnershipError)):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, (ProposalNotFoundError, MandateNotFoundError, PaymentOrderNotFoundError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if isinstance(
        exc,
        (
            PaymentProposalNotApprovedError,
            PaymentOrderNotApprovedError,
            PaymentBudgetExceededError,
            PaymentMandateInactiveError,
            MandateExpiredError,
            PaymentOrderAlreadySettledError,
            CurrencyMismatchError,
            ValueError,
        ),
    ):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, PayPalNetworkError):
        return HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc))
    if isinstance(exc, (PayPalAuthError, PayPalOrderError, PayPalCaptureError)):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    if isinstance(exc, PayPalConfigError):
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment gateway misconfiguration.",
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unexpected payment execution error.",
    )


@router.post(
    "/proposals/{proposal_id}/create-order",
    response_model=PaymentOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_payment_order_endpoint(
    proposal_id: str,
    body: PaymentOrderCreateRequest | None = None,
    session: UserSession = Depends(get_current_session),
) -> PaymentOrderResponse:
    """Create a PayPal Sandbox checkout order for an approved purchase proposal.

    Only proposals in APPROVED state are eligible.
    Ownership is strictly checked: authenticated principal must own the mandate.
    """
    if body is not None and body.proposal_id and body.proposal_id != proposal_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="proposal_id in body must match path parameter.",
        )

    return_url = body.return_url if body else None
    cancel_url = body.cancel_url if body else None

    try:
        return await create_payment_order(
            proposal_id=proposal_id,
            principal_id=session.user_id,
            return_url=return_url,
            cancel_url=cancel_url,
        )
    except Exception as exc:
        raise _handle_payment_exceptions(exc) from exc


@router.post(
    "/payments/{order_id}/capture",
    response_model=PaymentCaptureResponse,
    status_code=status.HTTP_200_OK,
)
async def capture_payment_order_endpoint(
    order_id: str,
    session: UserSession = Depends(get_current_session),
) -> PaymentCaptureResponse:
    """Capture an approved PayPal checkout order.

    Directly verifies buyer approval with PayPal Sandbox Orders API.
    Revalidates mandate ownership, active status, unexpired window, and budget.
    On successful capture, atomically settles proposal and logs audit entry.
    """
    try:
        return await capture_payment_order(
            order_id=order_id,
            principal_id=session.user_id,
        )
    except Exception as exc:
        raise _handle_payment_exceptions(exc) from exc


@router.get(
    "/payments/{order_id}",
    response_model=PaymentOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def get_payment_order_endpoint(
    order_id: str,
    session: UserSession = Depends(get_current_session),
) -> PaymentOrderResponse:
    """Retrieve details of a PayPal checkout order.

    Principal must own the mandate associated with this order.
    """
    try:
        return await get_payment_order(
            order_id=order_id,
            principal_id=session.user_id,
        )
    except Exception as exc:
        raise _handle_payment_exceptions(exc) from exc
