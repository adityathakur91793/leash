"""Payment Executor Service (Phase 3).

Orchestrates PayPal Sandbox checkout creation, buyer approval verification,
and order capture for policy-authorized purchase proposals.

Security Architecture:
- Proposal must be APPROVED and have a current durable purchase-consent binding.
- Mandate ownership is strictly enforced against authenticated session principal.
- Mandate must be ACTIVE and not expired.
- Budget constraints (total remaining, per-item, merchant/category whitelist) are rechecked.
- Browser return is untrusted: approval is verified directly against PayPal Orders API.
- Idempotency is enforced for both order creation and capture via atomic transactions.
- State transitions update paypal_orders and proposals (to SETTLED) atomically in SQLite.
- Tamper-evident audit logs record all order creation, capture, and failure events.
- Emergency kill switch halts all execution immediately.
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging

from app.db.database import (
    get_cumulative_mandate_spent,
    get_connection,
    get_mandate_from_db,
    get_paypal_order_by_proposal_from_db,
    get_paypal_order_from_db,
    get_proposal_from_db,
    insert_paypal_order_in_db,
    is_kill_switch_active,
    update_paypal_order_captured_in_db,
    update_paypal_order_status_in_db,
    update_proposal_status_in_db,
)
from app.schemas.audit import AuditActor, AuditEntityType, AuditEventType
from app.schemas.mandate import MandateStatus
from app.schemas.payment import (
    PaymentCaptureResponse,
    PaymentOrderResponse,
    PayPalOrderRecord,
    PayPalOrderStatus,
)
from app.schemas.proposal import ProposalStatus
from app.services.approval_service import MandateOwnershipError, OwnerlessMandateError
from app.services.audit_service import AuditService
from app.services.mandate_service import MandateExpiredError, MandateNotFoundError
from app.services.paypal_service import (
    PayPalCaptureError,
    PayPalClient,
    PayPalNetworkError,
    PayPalOrderError,
)
from app.services.proposal_service import CurrencyMismatchError, ProposalNotFoundError
from app.services.purchase_consent_service import require_current_purchase_consent

logger = logging.getLogger(__name__)


def _confirmed_capture(
    response: dict[str, object], expected_currency: str, expected_amount: Decimal
) -> tuple[str, datetime]:
    """Extract only an explicitly completed capture matching authorized terms."""
    if response.get("status") != "COMPLETED":
        raise PayPalCaptureError("PayPal has not confirmed a completed capture.")
    units = response.get("purchase_units")
    if not isinstance(units, list) or not units:
        raise PayPalCaptureError("PayPal capture response has no purchase unit.")
    unit = units[0]
    payments = unit.get("payments", {}) if isinstance(unit, dict) else {}
    captures = payments.get("captures", []) if isinstance(payments, dict) else []
    if not isinstance(captures, list) or not captures:
        raise PayPalCaptureError("PayPal capture response has no capture details.")
    capture = captures[0]
    if not isinstance(capture, dict) or capture.get("status") != "COMPLETED":
        raise PayPalCaptureError("PayPal capture status is not COMPLETED.")
    capture_id = capture.get("id")
    amount = capture.get("amount")
    if not isinstance(capture_id, str) or not capture_id.strip():
        raise PayPalCaptureError("PayPal capture response has no capture ID.")
    if not isinstance(amount, dict) or amount.get("currency_code") != expected_currency:
        raise PayPalCaptureError("PayPal capture currency does not match the approved proposal.")
    try:
        actual_amount = Decimal(str(amount.get("value")))
    except Exception as exc:
        raise PayPalCaptureError("PayPal capture amount is invalid.") from exc
    if actual_amount != expected_amount:
        raise PayPalCaptureError("PayPal capture amount does not match the approved proposal.")
    return capture_id, datetime.now(timezone.utc)


class PaymentOrderNotFoundError(Exception):
    """Raised when a referenced PayPal order cannot be found in the database."""


class PaymentOrderNotApprovedError(Exception):
    """Raised when capture is attempted on a PayPal order that has not been approved by buyer."""


class PaymentProposalNotApprovedError(Exception):
    """Raised when an order or capture is attempted on a proposal not in APPROVED status."""


class PaymentOrderAlreadySettledError(Exception):
    """Raised when an order creation is attempted on a proposal that has already been settled."""


class PaymentBudgetExceededError(Exception):
    """Raised when a proposal exceeds mandate limits or remaining budget during pre-flight."""


class PaymentMandateInactiveError(Exception):
    """Raised when the associated mandate is not in ACTIVE status."""


async def create_payment_order(
    proposal_id: str,
    principal_id: str,
    return_url: str | None = None,
    cancel_url: str | None = None,
    paypal_client: PayPalClient | None = None,
) -> PaymentOrderResponse:
    """Create a PayPal Sandbox checkout order for an approved purchase proposal.

    Args:
        proposal_id: Unique identifier of the proposal to execute payment for.
        principal_id: Authenticated principal ID attempting the creation.
        return_url: Optional client return URL after approval on PayPal.
        cancel_url: Optional client cancel URL if user cancels on PayPal.
        paypal_client: Optional PayPal client for testing/mock injection.

    Returns:
        PaymentOrderResponse with order details and validated PayPal approval URL.

    Raises:
        RuntimeError: If emergency kill switch is engaged.
        ProposalNotFoundError: If proposal does not exist.
        PaymentProposalNotApprovedError: If proposal is not in APPROVED status.
        MandateNotFoundError: If associated mandate does not exist.
        OwnerlessMandateError: If mandate lacks an owner.
        MandateOwnershipError: If principal does not own the mandate.
        PaymentMandateInactiveError: If mandate is not ACTIVE.
        MandateExpiredError: If mandate has expired.
        CurrencyMismatchError: If proposal currency does not match mandate.
        PaymentBudgetExceededError: If proposal exceeds budget or item constraints.
        PaymentOrderAlreadySettledError: If proposal is already settled.
        PayPalOrderError: If PayPal order creation fails.
    """
    if await is_kill_switch_active():
        raise RuntimeError("Emergency kill switch is active. Payment execution halted.")

    proposal = await get_proposal_from_db(proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(f"Proposal '{proposal_id}' not found.")

    if proposal.status != ProposalStatus.APPROVED:
        raise PaymentProposalNotApprovedError(
            f"Proposal '{proposal_id}' is in status '{proposal.status.value}', expected 'APPROVED'."
        )

    # Legacy APPROVED rows are not evidence of consent. Require the durable
    # consent and compare it to current policy/mandate/terms before PayPal calls.
    await require_current_purchase_consent(proposal_id, principal_id)

    mandate = await get_mandate_from_db(proposal.mandate_id)
    if mandate is None:
        raise MandateNotFoundError(f"Mandate '{proposal.mandate_id}' not found.")

    if not mandate.owner_user_id:
        raise OwnerlessMandateError(
            f"Mandate '{mandate.mandate_id}' has no assigned owner."
        )

    if mandate.owner_user_id != principal_id:
        raise MandateOwnershipError(
            f"Principal '{principal_id}' does not own mandate '{mandate.mandate_id}'."
        )

    if mandate.status != MandateStatus.ACTIVE:
        raise PaymentMandateInactiveError(
            f"Mandate '{mandate.mandate_id}' is not ACTIVE (status: {mandate.status.value})."
        )

    now = datetime.now(timezone.utc)
    if now >= mandate.expires_at:
        raise MandateExpiredError(f"Mandate '{mandate.mandate_id}' has expired.")

    # Revalidate policy constraints deterministically
    if proposal.currency != mandate.currency:
        raise CurrencyMismatchError(
            f"Proposal currency '{proposal.currency}' does not match "
            f"mandate currency '{mandate.currency}'."
        )

    cumulative_spent = await get_cumulative_mandate_spent(
        mandate.mandate_id, exclude_proposal_id=proposal.proposal_id
    )
    if cumulative_spent + proposal.total > mandate.max_total_budget:
        raise PaymentBudgetExceededError(
            f"Proposal total ({proposal.total}) exceeds remaining mandate budget "
            f"({mandate.max_total_budget - cumulative_spent})."
        )

    if mandate.max_price_per_item is not None and proposal.unit_price > mandate.max_price_per_item:
        raise PaymentBudgetExceededError(
            f"Unit price {proposal.unit_price} exceeds mandate max_price_per_item "
            f"{mandate.max_price_per_item}."
        )

    if mandate.max_item_count is not None and proposal.quantity > mandate.max_item_count:
        raise PaymentBudgetExceededError(
            f"Quantity {proposal.quantity} exceeds mandate max_item_count "
            f"{mandate.max_item_count}."
        )

    if (
        mandate.max_quantity_per_item is not None
        and proposal.quantity > mandate.max_quantity_per_item
    ):
        raise PaymentBudgetExceededError(
            f"Quantity {proposal.quantity} exceeds mandate max_quantity_per_item "
            f"{mandate.max_quantity_per_item}."
        )

    if mandate.allowed_merchants and proposal.merchant not in mandate.allowed_merchants:
        raise PaymentBudgetExceededError(
            f"Merchant '{proposal.merchant}' is not allowed by mandate."
        )

    if mandate.allowed_categories and proposal.category not in mandate.allowed_categories:
        raise PaymentBudgetExceededError(
            f"Category '{proposal.category}' is not allowed by mandate."
        )

    from app.services.intent_audit_service import require_current_assessment
    await require_current_assessment(proposal, mandate, require_human_review=True)

    # Check for existing PayPal order for this proposal (Idempotency)
    existing_order = await get_paypal_order_by_proposal_from_db(proposal_id)
    if existing_order is not None:
        if existing_order.status == PayPalOrderStatus.COMPLETED:
            raise PaymentOrderAlreadySettledError(
                f"Proposal '{proposal_id}' is already settled under PayPal order "
                f"'{existing_order.order_id}'."
            )
        if existing_order.status in (PayPalOrderStatus.CREATED, PayPalOrderStatus.APPROVED):
            return PaymentOrderResponse(
                order_id=existing_order.order_id,
                proposal_id=existing_order.proposal_id,
                mandate_id=existing_order.mandate_id,
                owner_user_id=existing_order.owner_user_id,
                status=existing_order.status,
                currency=existing_order.currency,
                total_amount=existing_order.total_amount,
                approval_url=existing_order.approval_url,
                created_at=existing_order.created_at,
            )

    client = paypal_client or PayPalClient()
    paypal_request_id = f"ord_{proposal.proposal_id}"
    ret_url = return_url or "http://localhost:5173/checkout/complete"
    can_url = cancel_url or "http://localhost:5173/checkout/cancel"

    order_result = await client.create_order(
        proposal=proposal,
        mandate=mandate,
        paypal_request_id=paypal_request_id,
        return_url=ret_url,
        cancel_url=can_url,
    )

    paypal_order_id = str(order_result["order_id"])
    approval_url = str(order_result["approval_url"])
    status_raw = order_result.get("status", PayPalOrderStatus.CREATED.value)
    order_status = (
        PayPalOrderStatus(status_raw)
        if status_raw in PayPalOrderStatus._value2member_map_
        else PayPalOrderStatus.CREATED
    )

    order_record = PayPalOrderRecord(
        order_id=paypal_order_id,
        proposal_id=proposal.proposal_id,
        mandate_id=mandate.mandate_id,
        owner_user_id=principal_id,
        status=order_status,
        currency=proposal.currency,
        total_amount=proposal.total,
        subtotal=proposal.subtotal,
        tax=proposal.tax,
        shipping=proposal.shipping,
        other_charges=proposal.other_charges,
        approval_url=approval_url,
        paypal_request_id=paypal_request_id,
        created_at=now,
        updated_at=now,
    )

    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if await is_kill_switch_active(db=conn):
                raise RuntimeError("Emergency kill switch is active. Payment execution halted.")
            current_proposal = await get_proposal_from_db(proposal_id, db=conn)
            current_mandate = await get_mandate_from_db(mandate.mandate_id, db=conn)
            if current_proposal is None or current_mandate is None:
                raise PaymentProposalNotApprovedError("Proposal authorization changed.")
            await require_current_purchase_consent(
                proposal_id, principal_id, db=conn
            )
            await require_current_assessment(
                current_proposal, current_mandate, db=conn, require_human_review=True
            )
            await insert_paypal_order_in_db(order_record, db=conn)
            await AuditService.append_event(
                event_type=AuditEventType.PAYMENT_ORDER_CREATED,
                entity_type=AuditEntityType.PAYMENT,
                entity_id=paypal_order_id,
                actor=AuditActor.SYSTEM,
                payload={
                    "order_id": paypal_order_id,
                    "proposal_id": proposal.proposal_id,
                    "mandate_id": mandate.mandate_id,
                    "total": str(proposal.total),
                    "currency": proposal.currency,
                    "principal_id": principal_id,
                },
                db=conn,
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    return PaymentOrderResponse(
        order_id=order_record.order_id,
        proposal_id=order_record.proposal_id,
        mandate_id=order_record.mandate_id,
        owner_user_id=order_record.owner_user_id,
        status=order_record.status,
        currency=order_record.currency,
        total_amount=order_record.total_amount,
        approval_url=order_record.approval_url,
        created_at=order_record.created_at,
    )


async def capture_payment_order(
    order_id: str,
    principal_id: str,
    paypal_client: PayPalClient | None = None,
) -> PaymentCaptureResponse:
    """Capture an approved PayPal checkout order after revalidating all authorization terms.

    Args:
        order_id: Unique PayPal order ID to capture.
        principal_id: Authenticated principal attempting the capture.
        paypal_client: Optional PayPal client for testing/mock injection.

    Returns:
        PaymentCaptureResponse confirming settlement and receipt.

    Raises:
        RuntimeError: If emergency kill switch is active.
        PaymentOrderNotFoundError: If order does not exist in LEASH.
        MandateOwnershipError: If caller does not own the order or parent mandate.
        OwnerlessMandateError: If mandate lacks an owner.
        PaymentProposalNotApprovedError: If proposal is not APPROVED.
        PaymentMandateInactiveError: If mandate is not ACTIVE.
        MandateExpiredError: If mandate has expired.
        PaymentBudgetExceededError: If capture would exceed remaining budget.
        PaymentOrderNotApprovedError: If PayPal reports order is not approved by buyer.
        PayPalCaptureError: If capture fails on PayPal's side.
    """
    if await is_kill_switch_active():
        raise RuntimeError("Emergency kill switch is active. Payment execution halted.")

    order_record = await get_paypal_order_from_db(order_id)
    if order_record is None:
        raise PaymentOrderNotFoundError(f"PayPal order '{order_id}' not found.")

    if order_record.owner_user_id != principal_id:
        raise MandateOwnershipError(
            f"Principal '{principal_id}' does not own payment order '{order_id}'."
        )

    # Idempotent response if already completed
    if order_record.status == PayPalOrderStatus.COMPLETED and order_record.capture_id:
        return PaymentCaptureResponse(
            order_id=order_record.order_id,
            proposal_id=order_record.proposal_id,
            mandate_id=order_record.mandate_id,
            capture_id=order_record.capture_id,
            status=PayPalOrderStatus.COMPLETED,
            currency=order_record.currency,
            total_amount=order_record.total_amount,
            captured_at=order_record.captured_at or order_record.updated_at,
            receipt={
                "order_id": order_record.order_id,
                "capture_id": order_record.capture_id,
                "status": "COMPLETED",
                "amount": str(order_record.total_amount),
                "currency": order_record.currency,
            },
        )

    if order_record.status in (PayPalOrderStatus.FAILED, PayPalOrderStatus.CANCELLED):
        raise ValueError(
            f"Order '{order_id}' cannot be captured (status: {order_record.status.value})."
        )

    if order_record.status == PayPalOrderStatus.RECONCILIATION_REQUIRED:
        raise PayPalCaptureError(
            "Capture outcome is uncertain; order requires explicit reconciliation."
        )

    proposal = await get_proposal_from_db(order_record.proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(f"Proposal '{order_record.proposal_id}' not found.")

    mandate = await get_mandate_from_db(order_record.mandate_id)
    if mandate is None:
        raise MandateNotFoundError(f"Mandate '{order_record.mandate_id}' not found.")

    if not mandate.owner_user_id:
        raise OwnerlessMandateError(
            f"Mandate '{mandate.mandate_id}' has no assigned owner."
        )

    if mandate.owner_user_id != principal_id:
        raise MandateOwnershipError(
            f"Principal '{principal_id}' does not own mandate '{mandate.mandate_id}'."
        )

    if mandate.status != MandateStatus.ACTIVE:
        raise PaymentMandateInactiveError(
            f"Mandate '{mandate.mandate_id}' is not ACTIVE (status: {mandate.status.value})."
        )

    now = datetime.now(timezone.utc)
    if now >= mandate.expires_at:
        raise MandateExpiredError(f"Mandate '{mandate.mandate_id}' has expired.")

    if proposal.status != ProposalStatus.APPROVED:
        raise PaymentProposalNotApprovedError(
            f"Proposal '{proposal.proposal_id}' status is '{proposal.status.value}', "
            f"expected 'APPROVED'."
        )

    await require_current_purchase_consent(order_record.proposal_id, principal_id)

    from app.services.intent_audit_service import require_current_assessment
    await require_current_assessment(proposal, mandate, require_human_review=True)

    # TOCTOU Budget revalidation immediately prior to execution
    cumulative_spent = await get_cumulative_mandate_spent(
        mandate.mandate_id, exclude_proposal_id=proposal.proposal_id
    )
    if cumulative_spent + proposal.total > mandate.max_total_budget:
        raise PaymentBudgetExceededError(
            f"Payment capture exceeds remaining mandate budget "
            f"({mandate.max_total_budget - cumulative_spent})."
        )

    # Untrusted browser return: query PayPal directly to verify buyer approval
    client = paypal_client or PayPalClient()
    paypal_order_data = await client.get_order(order_id)
    paypal_status = paypal_order_data.get("status")

    if paypal_status not in ("APPROVED", "COMPLETED"):
        raise PaymentOrderNotApprovedError(
            f"PayPal order '{order_id}' is not approved by buyer "
            f"(current PayPal status: '{paypal_status}')."
        )

    # Re-verify amount and currency from PayPal response against proposal
    purchase_units = paypal_order_data.get("purchase_units", [])
    if not purchase_units:
        raise ValueError(f"PayPal order '{order_id}' contains no purchase units.")

    amount_obj = purchase_units[0].get("amount", {})
    currency_code = amount_obj.get("currency_code")
    amount_str = amount_obj.get("value")

    if currency_code != proposal.currency:
        raise ValueError(
            f"PayPal order currency '{currency_code}' does not match proposal "
            f"currency '{proposal.currency}'."
        )

    if Decimal(amount_str) != proposal.total:
        raise ValueError(
            f"PayPal order amount '{amount_str}' does not match proposal total '{proposal.total}'."
        )

    capture_req_id = f"cap_req_{order_id}"
    uncertain_outcome = False
    capture_id: str | None = None
    now_utc = datetime.now(timezone.utc)

    # Serialize authorization changes against the external capture request and
    # keep both local state transitions and the audit append in this transaction.
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if await is_kill_switch_active(db=conn):
                raise RuntimeError("Emergency kill switch is active. Payment execution halted.")
            current_order = await get_paypal_order_from_db(order_id, db=conn)
            if current_order is None:
                raise PaymentOrderNotFoundError(
                    f"PayPal order '{order_id}' not found during capture authorization."
                )
            if current_order.owner_user_id != principal_id:
                raise MandateOwnershipError(
                    f"Principal '{principal_id}' does not own payment order '{order_id}'."
                )
            if current_order.status == PayPalOrderStatus.COMPLETED:
                if not current_order.capture_id:
                    raise PayPalCaptureError(
                        "Completed PayPal order has no verified capture ID."
                    )
                return PaymentCaptureResponse(
                    order_id=current_order.order_id,
                    proposal_id=current_order.proposal_id,
                    mandate_id=current_order.mandate_id,
                    capture_id=current_order.capture_id,
                    status=PayPalOrderStatus.COMPLETED,
                    currency=current_order.currency,
                    total_amount=current_order.total_amount,
                    captured_at=current_order.captured_at or current_order.updated_at,
                    receipt={
                        "order_id": current_order.order_id,
                        "capture_id": current_order.capture_id,
                        "status": "COMPLETED",
                        "amount": str(current_order.total_amount),
                        "currency": current_order.currency,
                    },
                )
            if current_order.status in (
                PayPalOrderStatus.FAILED,
                PayPalOrderStatus.CANCELLED,
            ):
                raise ValueError(
                    f"Order '{order_id}' cannot be captured "
                    f"(status: {current_order.status.value})."
                )
            if current_order.status == PayPalOrderStatus.RECONCILIATION_REQUIRED:
                raise PayPalCaptureError(
                    "Capture outcome is uncertain; order requires explicit reconciliation."
                )
            if current_order.status not in (
                PayPalOrderStatus.CREATED,
                PayPalOrderStatus.APPROVED,
            ):
                raise PayPalCaptureError(
                    f"Order '{order_id}' is not in a capturable state."
                )
            order_record = current_order
            current_proposal = await get_proposal_from_db(proposal.proposal_id, db=conn)
            current_mandate = await get_mandate_from_db(mandate.mandate_id, db=conn)
            if current_proposal is None or current_mandate is None:
                raise PaymentProposalNotApprovedError("Proposal authorization changed before capture.")
            await require_current_purchase_consent(
                current_proposal.proposal_id, principal_id, db=conn
            )
            from app.services.intent_audit_service import require_current_assessment
            await require_current_assessment(
                current_proposal, current_mandate, db=conn, require_human_review=True
            )
            if paypal_status == "APPROVED":
                if await is_kill_switch_active(db=conn):
                    raise RuntimeError("Emergency kill switch is active. Payment execution halted.")
                current_proposal = await get_proposal_from_db(proposal.proposal_id, db=conn)
                current_mandate = await get_mandate_from_db(mandate.mandate_id, db=conn)
                if current_proposal is None or current_proposal.status != ProposalStatus.APPROVED:
                    raise PaymentProposalNotApprovedError(
                        "Proposal authorization changed before capture."
                    )
                if current_mandate is None or current_mandate.status != MandateStatus.ACTIVE:
                    raise PaymentMandateInactiveError(
                        "Mandate authorization changed before capture."
                    )
                if (
                    current_mandate.owner_user_id != principal_id
                    or current_mandate.currency != current_proposal.currency
                    or datetime.now(timezone.utc) >= current_mandate.expires_at
                ):
                    raise PaymentProposalNotApprovedError(
                        "Mandate authorization changed before capture."
                    )
                current_spend = await get_cumulative_mandate_spent(
                    current_mandate.mandate_id,
                    exclude_proposal_id=current_proposal.proposal_id,
                    db=conn,
                )
                if current_spend + current_proposal.total > current_mandate.max_total_budget:
                    raise PaymentBudgetExceededError(
                        "Payment capture exceeds the current remaining mandate budget."
                    )
                if (
                    current_mandate.max_price_per_item is not None
                    and current_proposal.unit_price > current_mandate.max_price_per_item
                ) or (
                    current_mandate.max_item_count is not None
                    and current_proposal.quantity > current_mandate.max_item_count
                ) or (
                    current_mandate.max_quantity_per_item is not None
                    and current_proposal.quantity > current_mandate.max_quantity_per_item
                ):
                    raise PaymentBudgetExceededError(
                        "Proposal exceeds current mandate item limits."
                    )
                if (
                    current_mandate.allowed_merchants
                    and current_proposal.merchant not in current_mandate.allowed_merchants
                ) or (
                    current_mandate.allowed_categories
                    and current_proposal.category not in current_mandate.allowed_categories
                ):
                    raise PaymentBudgetExceededError(
                        "Proposal is outside the current mandate allowlist."
                    )
                try:
                    capture_result = await client.capture_order(order_id, capture_req_id)
                except (PayPalNetworkError, PayPalCaptureError) as exc:
                    # Reconcile before any later attempt; never retry in this call.
                    try:
                        reconciled = await client.get_order(order_id)
                    except Exception:
                        reconciled = {}
                    if reconciled.get("status") == "COMPLETED":
                        capture_result = reconciled
                    else:
                        marked = await update_paypal_order_status_in_db(
                            order_id,
                            PayPalOrderStatus.RECONCILIATION_REQUIRED,
                            datetime.now(timezone.utc).isoformat(),
                            db=conn,
                        )
                        if not marked:
                            raise RuntimeError(
                                "Could not persist uncertain PayPal capture state."
                            ) from exc
                        await AuditService.append_event(
                            event_type=AuditEventType.PAYMENT_FAILED,
                            entity_type=AuditEntityType.PAYMENT,
                            entity_id=order_id,
                            actor=AuditActor.SYSTEM,
                            payload={
                                "order_id": order_id,
                                "proposal_id": proposal.proposal_id,
                                "mandate_id": mandate.mandate_id,
                                "outcome": "RECONCILIATION_REQUIRED",
                            },
                            db=conn,
                        )
                        await conn.commit()
                        uncertain_outcome = True
                        capture_result = {}
            else:
                # PayPal COMPLETED is authoritative after an earlier ambiguous response.
                capture_result = paypal_order_data

            if uncertain_outcome:
                pass
            else:
                raw_capture = capture_result.get("raw_response", capture_result)
                if not isinstance(raw_capture, dict):
                    raise PayPalCaptureError("PayPal capture response is malformed.")
                capture_id, now_utc = _confirmed_capture(
                    raw_capture, proposal.currency, proposal.total
                )
                now_iso = now_utc.isoformat()

            order_updated = await update_paypal_order_captured_in_db(
                order_id=order_id,
                capture_id=capture_id or "",
                capture_request_id=capture_req_id,
                captured_at_iso=now_utc.isoformat(),
                updated_at_iso=now_utc.isoformat(),
                db=conn,
            ) if not uncertain_outcome else True

            if not uncertain_outcome:
                if not order_updated:
                    raise RuntimeError("Payment order changed during capture finalization.")
                proposal_updated = await update_proposal_status_in_db(
                    proposal_id=proposal.proposal_id,
                    new_status=ProposalStatus.SETTLED,
                    expected_current_status=ProposalStatus.APPROVED,
                    db=conn,
                )
                if not proposal_updated:
                    raise RuntimeError("Proposal changed during capture finalization.")

                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_CAPTURED,
                    entity_type=AuditEntityType.PAYMENT,
                    entity_id=order_id,
                    actor=AuditActor.SYSTEM,
                    payload={
                        "order_id": order_id,
                        "capture_id": capture_id,
                        "proposal_id": proposal.proposal_id,
                        "mandate_id": mandate.mandate_id,
                        "total": str(proposal.total),
                        "currency": proposal.currency,
                        "principal_id": principal_id,
                    },
                    db=conn,
                )
                await conn.commit()
        except Exception:
            if not uncertain_outcome:
                await conn.rollback()
            raise

    if uncertain_outcome:
        raise PayPalCaptureError(
            "Capture outcome is uncertain; order requires reconciliation."
        )

    return PaymentCaptureResponse(
        order_id=order_id,
        proposal_id=proposal.proposal_id,
        mandate_id=mandate.mandate_id,
        capture_id=capture_id or "",
        status=PayPalOrderStatus.COMPLETED,
        currency=proposal.currency,
        total_amount=proposal.total,
        captured_at=now_utc,
        receipt={
            "order_id": order_id,
            "capture_id": capture_id,
            "status": "COMPLETED",
            "amount": str(proposal.total),
            "currency": proposal.currency,
        },
    )


async def get_payment_order(
    order_id: str,
    principal_id: str,
) -> PaymentOrderResponse:
    """Retrieve details of a PayPal checkout order, enforcing principal ownership.

    Args:
        order_id: PayPal order identifier.
        principal_id: Authenticated principal querying the order.

    Returns:
        PaymentOrderResponse for the queried order.

    Raises:
        PaymentOrderNotFoundError: If order does not exist in LEASH.
        MandateOwnershipError: If principal does not own the order.
    """
    order_record = await get_paypal_order_from_db(order_id)
    if order_record is None:
        raise PaymentOrderNotFoundError(f"PayPal order '{order_id}' not found.")

    if order_record.owner_user_id != principal_id:
        raise MandateOwnershipError(
            f"Principal '{principal_id}' does not own payment order '{order_id}'."
        )

    return PaymentOrderResponse(
        order_id=order_record.order_id,
        proposal_id=order_record.proposal_id,
        mandate_id=order_record.mandate_id,
        owner_user_id=order_record.owner_user_id,
        status=order_record.status,
        currency=order_record.currency,
        total_amount=order_record.total_amount,
        approval_url=order_record.approval_url,
        created_at=order_record.created_at,
    )
