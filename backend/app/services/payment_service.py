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

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import re
import uuid

from app.db.database import (
    get_cumulative_mandate_spent,
    get_connection,
    get_mandate_from_db,
    get_paypal_order_by_proposal_from_db,
    get_paypal_order_from_db,
    get_paypal_attempt_by_proposal,
    get_proposal_from_db,
    insert_paypal_attempt,
    insert_paypal_order_in_db,
    is_kill_switch_active,
    update_paypal_order_captured_in_db,
    update_paypal_order_status_in_db,
    transition_paypal_attempt,
    update_proposal_status_in_db,
)
from app.schemas.audit import AuditActor, AuditEntityType, AuditEventType
from app.schemas.mandate import MandateStatus
from app.schemas.payment import (
    OrderAttemptStatus,
    PaymentCaptureResponse,
    PaymentOrderResponse,
    PayPalOrderRecord,
    PayPalOrderStatus,
    PayPalOrderAttempt,
    PaymentRecoveryResponse,
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

ORDER_REQUEST_LEASE = timedelta(minutes=2)

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


def _validate_provider_binding(
    data: object, proposal_id: str, expected_currency: str, expected_amount: Decimal,
) -> None:
    """Validate provider order identity and amount against persisted LEASH terms."""
    if not isinstance(data, dict) or data.get("status") != "COMPLETED":
        raise PayPalCaptureError("PayPal has not confirmed a completed order.")
    units = data.get("purchase_units")
    if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], dict):
        raise PayPalCaptureError("PayPal completed order has invalid purchase units.")
    unit = units[0]
    amount = unit.get("amount")
    if (unit.get("reference_id") != proposal_id or not isinstance(amount, dict)
            or amount.get("currency_code") != expected_currency):
        raise PayPalCaptureError("PayPal completed order does not match its LEASH binding.")
    try:
        matches = Decimal(str(amount.get("value"))) == expected_amount
    except Exception:
        matches = False
    if not matches:
        raise PayPalCaptureError("PayPal completed order amount does not match LEASH terms.")


async def _mark_capture_reconciliation_required(order_id: str, proposal_id: str,
                                                reason: str) -> None:
    """Persist a fail-closed state when provider completion cannot be verified."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            current = await get_paypal_order_from_db(order_id, db=conn)
            if current and current.status == PayPalOrderStatus.COMPLETED \
                    and not current.capture_id:
                cursor = await conn.execute(
                    "UPDATE paypal_orders SET status=?, updated_at=? "
                    "WHERE order_id=? AND status=? AND capture_id IS NULL",
                    (PayPalOrderStatus.RECONCILIATION_REQUIRED.value,
                     datetime.now(timezone.utc).isoformat(), order_id,
                     PayPalOrderStatus.COMPLETED.value),
                )
                changed = cursor.rowcount == 1
            else:
                changed = await update_paypal_order_status_in_db(
                    order_id, PayPalOrderStatus.RECONCILIATION_REQUIRED,
                    datetime.now(timezone.utc).isoformat(), db=conn,
                )
            if changed:
                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_FAILED,
                    entity_type=AuditEntityType.PAYMENT, entity_id=order_id,
                    actor=AuditActor.SYSTEM,
                    payload={"order_id": order_id, "proposal_id": proposal_id,
                             "outcome": "RECONCILIATION_REQUIRED", "reason": reason}, db=conn,
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def _reconcile_completed_capture(
    order_record: PayPalOrderRecord, provider_order: dict[str, object],
    principal_id: str,
) -> PaymentCaptureResponse:
    """Record provider-confirmed settlement as a fact, without authorizing new capture."""
    try:
        if provider_order.get("id") != order_record.order_id:
            raise PayPalCaptureError("PayPal returned a different order during reconciliation.")
        _validate_provider_binding(
            provider_order, order_record.proposal_id,
            order_record.currency, order_record.total_amount,
        )
        capture_id, captured_at = _confirmed_capture(
            provider_order, order_record.currency, order_record.total_amount,
        )
    except PayPalCaptureError:
        await _mark_capture_reconciliation_required(
            order_record.order_id, order_record.proposal_id,
            "provider_completed_evidence_invalid",
        )
        raise

    captured_iso = captured_at.isoformat()
    capture_request_id = f"cap_req_{order_record.order_id}"
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            current = await get_paypal_order_from_db(order_record.order_id, db=conn)
            if (current is None or current.proposal_id != order_record.proposal_id
                    or current.owner_user_id != principal_id
                    or current.currency != order_record.currency
                    or current.total_amount != order_record.total_amount):
                raise PayPalCaptureError("Local order binding changed during reconciliation.")
            if current.status == PayPalOrderStatus.COMPLETED and current.capture_id:
                if current.capture_id != capture_id:
                    raise PayPalCaptureError("PayPal capture ID conflicts with persisted settlement.")
                capture_id = current.capture_id
                captured_at = current.captured_at or current.updated_at
                await conn.commit()
            else:
                updated = await update_paypal_order_captured_in_db(
                    current.order_id, capture_id, capture_request_id,
                    captured_iso, captured_iso, db=conn,
                )
                if not updated:
                    raise PayPalCaptureError("Completed capture could not be persisted safely.")
                current_proposal = await get_proposal_from_db(current.proposal_id, db=conn)
                if current_proposal is None:
                    raise ProposalNotFoundError("Proposal disappeared during payment reconciliation.")
                if current_proposal.status != ProposalStatus.SETTLED:
                    changed = await update_proposal_status_in_db(
                        current.proposal_id, ProposalStatus.SETTLED,
                        expected_current_status=current_proposal.status, db=conn,
                    )
                    if not changed:
                        raise PayPalCaptureError("Proposal settlement state changed during reconciliation.")
                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_CAPTURED,
                    entity_type=AuditEntityType.PAYMENT, entity_id=current.order_id,
                    actor=AuditActor.SYSTEM,
                    payload={"order_id": current.order_id, "capture_id": capture_id,
                             "proposal_id": current.proposal_id,
                             "mandate_id": current.mandate_id,
                             "total": str(current.total_amount), "currency": current.currency,
                             "principal_id": principal_id, "reconciled": True}, db=conn,
                )
                attempt = await get_paypal_attempt_by_proposal(current.proposal_id, db=conn)
                if attempt is not None and attempt.status != OrderAttemptStatus.PROVIDER_CREATED:
                    await transition_paypal_attempt(
                        attempt.attempt_id, attempt.status, OrderAttemptStatus.PROVIDER_CREATED,
                        datetime.now(timezone.utc), provider_order_id=current.order_id,
                        approval_url=current.approval_url, db=conn,
                    )
                await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    return PaymentCaptureResponse(
        order_id=order_record.order_id, proposal_id=order_record.proposal_id,
        mandate_id=order_record.mandate_id, capture_id=capture_id,
        status=PayPalOrderStatus.COMPLETED, currency=order_record.currency,
        total_amount=order_record.total_amount, captured_at=captured_at,
        receipt={"order_id": order_record.order_id, "capture_id": capture_id,
                 "status": "COMPLETED", "amount": str(order_record.total_amount),
                 "currency": order_record.currency},
    )


async def _recover_completed_attempt(
    proposal, attempt: PayPalOrderAttempt, principal_id: str, client: PayPalClient,
    *, provider_order: dict[str, object] | None = None,
) -> PaymentOrderResponse | None:
    """Recover a completed provider order before ordinary authorization checks.

    This path records external payment truth only. It never creates or captures an
    order and is reachable only through the persisted, owner-bound attempt ID.
    """
    if attempt.owner_user_id != principal_id:
        raise MandateOwnershipError("The current session does not own this checkout.")
    if attempt.proposal_id != proposal.proposal_id or not attempt.provider_order_id:
        return None
    local_order = await get_paypal_order_by_proposal_from_db(proposal.proposal_id)
    if local_order is not None:
        return None

    if provider_order is None:
        provider_order = await client.get_order(attempt.provider_order_id)
    if not isinstance(provider_order, dict) or provider_order.get("status") != "COMPLETED":
        return None
    try:
        if provider_order.get("id") != attempt.provider_order_id:
            raise PayPalCaptureError("Provider order ID does not match its durable attempt.")
        _validate_provider_binding(
            provider_order, attempt.proposal_id, attempt.expected_currency,
            attempt.expected_amount,
        )
        _confirmed_capture(provider_order, attempt.expected_currency, attempt.expected_amount)
    except PayPalCaptureError:
        async with get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                current_attempt = await get_paypal_attempt_by_proposal(
                    attempt.proposal_id, db=conn
                )
                if current_attempt and current_attempt.status != OrderAttemptStatus.RECONCILIATION_REQUIRED:
                    changed = await transition_paypal_attempt(
                        current_attempt.attempt_id, current_attempt.status,
                        OrderAttemptStatus.RECONCILIATION_REQUIRED,
                        datetime.now(timezone.utc), db=conn,
                    )
                    if changed:
                        await AuditService.append_event(
                            event_type=AuditEventType.PAYMENT_ORDER_ATTEMPT_UNKNOWN,
                            entity_type=AuditEntityType.PAYMENT,
                            entity_id=current_attempt.attempt_id, actor=AuditActor.SYSTEM,
                            payload={"proposal_id": attempt.proposal_id,
                                     "attempt_id": attempt.attempt_id,
                                     "reason": "completed_provider_evidence_invalid"}, db=conn,
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        raise

    now = datetime.now(timezone.utc)
    recovered_order = PayPalOrderRecord(
        order_id=attempt.provider_order_id, proposal_id=attempt.proposal_id,
        mandate_id=proposal.mandate_id, owner_user_id=principal_id,
        status=PayPalOrderStatus.RECONCILIATION_REQUIRED,
        currency=attempt.expected_currency, total_amount=attempt.expected_amount,
        subtotal=proposal.subtotal, tax=proposal.tax, shipping=proposal.shipping,
        other_charges=proposal.other_charges, approval_url=attempt.approval_url or "",
        paypal_request_id=attempt.provider_request_id, created_at=attempt.created_at,
        updated_at=now,
    )
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            existing = await get_paypal_order_by_proposal_from_db(
                attempt.proposal_id, db=conn
            )
            if existing is None:
                await insert_paypal_order_in_db(recovered_order, db=conn)
                current_attempt = await get_paypal_attempt_by_proposal(
                    attempt.proposal_id, db=conn
                )
                if current_attempt and current_attempt.status != OrderAttemptStatus.PROVIDER_CREATED:
                    await transition_paypal_attempt(
                        current_attempt.attempt_id, current_attempt.status,
                        OrderAttemptStatus.PROVIDER_CREATED, now,
                        provider_order_id=attempt.provider_order_id,
                        approval_url=attempt.approval_url, db=conn,
                    )
                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_ORDER_ATTEMPT_RECONCILED,
                    entity_type=AuditEntityType.PAYMENT, entity_id=attempt.attempt_id,
                    actor=AuditActor.SYSTEM,
                    payload={"proposal_id": attempt.proposal_id,
                             "order_id": attempt.provider_order_id,
                             "outcome": "completed_provider_order_recovered"}, db=conn,
                )
            else:
                recovered_order = existing
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    captured = await _reconcile_completed_capture(recovered_order, provider_order, principal_id)
    return PaymentOrderResponse(
        order_id=captured.order_id, proposal_id=captured.proposal_id,
        status=PayPalOrderStatus.COMPLETED, currency=captured.currency,
        total_amount=captured.total_amount, approval_url=recovered_order.approval_url,
        created_at=recovered_order.created_at,
    )


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


def _validate_provider_creation_response(result: object, proposal) -> None:
    if not isinstance(result, dict):
        raise PayPalOrderError("PayPal order response is malformed.")
    order_id = result.get("order_id")
    approval_url = result.get("approval_url")
    if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
        raise PayPalOrderError("PayPal order response has an invalid order ID.")
    if not isinstance(approval_url, str) or len(approval_url) > 2048:
        raise PayPalOrderError("PayPal order response has an invalid approval URL.")
    PayPalClient._validate_approval_url(approval_url)
    status = result.get("status", "CREATED")
    if status not in ("CREATED", "PAYER_ACTION_REQUIRED", "APPROVED"):
        raise PayPalOrderError("PayPal order response has an unexpected creation status.")
    raw = result.get("raw_response")
    if raw is not None:
        if not isinstance(raw, dict) or raw.get("id") != order_id:
            raise PayPalOrderError("PayPal order response identity does not match its payload.")
        _validate_provider_order(raw, proposal)


def _validate_provider_order(data: object, proposal) -> None:
    if not isinstance(data, dict) or data.get("status") not in (
        "CREATED", "PAYER_ACTION_REQUIRED", "APPROVED", "COMPLETED"
    ):
        raise PayPalOrderError("PayPal order could not be reconciled to a checkout state.")
    units = data.get("purchase_units")
    if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], dict):
        raise PayPalOrderError("PayPal order has an invalid purchase unit.")
    unit = units[0]
    amount = unit.get("amount")
    if (unit.get("reference_id") != proposal.proposal_id or not isinstance(amount, dict)
            or amount.get("currency_code") != proposal.currency):
        raise PayPalOrderError("PayPal order does not match the proposal binding.")
    try:
        matches = Decimal(str(amount.get("value"))) == proposal.total
    except Exception:
        matches = False
    if not matches:
        raise PayPalOrderError("PayPal order amount does not match the proposal.")


async def _persist_reconciled_provider_order(
    attempt: PayPalOrderAttempt, proposal, mandate, principal_id: str,
    provider_order: dict[str, object], *, conn=None,
) -> PaymentOrderResponse:
    _validate_provider_order(provider_order, proposal)
    order_id = provider_order.get("id")
    if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
        raise PayPalOrderError("Reconciled PayPal order has no valid ID.")
    if attempt.provider_order_id and order_id != attempt.provider_order_id:
        raise PayPalOrderError("Reconciled PayPal order ID does not match its attempt.")
    links = provider_order.get("links")
    approval_url = attempt.approval_url
    if not approval_url and isinstance(links, list):
        approval_url = next((link.get("href") for link in links
                             if isinstance(link, dict) and link.get("rel") == "approve"), None)
    if not isinstance(approval_url, str):
        raise PayPalOrderError("Reconciled order is missing its approval URL.")
    client = PayPalClient()
    PayPalClient._validate_approval_url(approval_url)
    now = datetime.now(timezone.utc)
    order = PayPalOrderRecord(
        order_id=order_id, proposal_id=proposal.proposal_id, mandate_id=mandate.mandate_id,
        owner_user_id=principal_id,
        status=PayPalOrderStatus(provider_order.get("status", "CREATED")),
        currency=proposal.currency, total_amount=proposal.total, subtotal=proposal.subtotal,
        tax=proposal.tax, shipping=proposal.shipping, other_charges=proposal.other_charges,
        approval_url=approval_url, paypal_request_id=attempt.provider_request_id,
        created_at=now, updated_at=now,
    )
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        current_proposal = await get_proposal_from_db(proposal.proposal_id, db=db)
        current_mandate = await get_mandate_from_db(mandate.mandate_id, db=db)
        try:
            await require_current_purchase_consent(proposal.proposal_id, principal_id, db=db)
            from app.services.intent_audit_service import require_current_assessment
            if not current_proposal or not current_mandate:
                raise PaymentProposalNotApprovedError("Authorization is no longer current.")
            await require_current_assessment(
                current_proposal, current_mandate, db=db, require_human_review=True
            )
            existing = await get_paypal_order_by_proposal_from_db(proposal.proposal_id, db=db)
            if existing is None:
                await insert_paypal_order_in_db(order, db=db)
            elif (existing.order_id != order_id or existing.owner_user_id != principal_id
                  or existing.total_amount != proposal.total
                  or existing.currency != proposal.currency):
                raise PayPalOrderError("Existing local order conflicts with provider recovery.")
            current_attempt = await get_paypal_attempt_by_proposal(
                proposal.proposal_id, db=db
            )
            if current_attempt is None or current_attempt.attempt_id != attempt.attempt_id:
                raise PayPalOrderError("Durable order attempt changed during recovery.")
            attempt_transitioned = False
            if current_attempt.status != OrderAttemptStatus.PROVIDER_CREATED:
                attempt_transitioned = await transition_paypal_attempt(
                    current_attempt.attempt_id, current_attempt.status,
                    OrderAttemptStatus.PROVIDER_CREATED, now,
                    provider_order_id=order_id, approval_url=approval_url, db=db,
                )
                if not attempt_transitioned:
                    raise PayPalOrderError("Could not persist recovered provider order state.")
            if existing is None or attempt_transitioned:
                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_ORDER_ATTEMPT_RECONCILED,
                    entity_type=AuditEntityType.PAYMENT, entity_id=attempt.attempt_id,
                    actor=AuditActor.SYSTEM,
                    payload={"proposal_id": proposal.proposal_id, "order_id": order_id,
                             "outcome": "provider_order_found"}, db=db,
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return PaymentOrderResponse(
        order_id=order_id, proposal_id=proposal.proposal_id, status=order.status,
        currency=proposal.currency, total_amount=proposal.total,
        approval_url=approval_url, created_at=now,
    )


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
    proposal = await get_proposal_from_db(proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(f"Proposal '{proposal_id}' not found.")

    # If the provider order ID was durably anchored but local order persistence
    # did not finish, first look for a completed external capture. This is factual
    # reconciliation only; no new order/capture is dispatched by this path.
    existing_attempt = await get_paypal_attempt_by_proposal(proposal_id)
    if existing_attempt is not None and existing_attempt.provider_order_id:
        recovery_client = paypal_client or PayPalClient()
        recovered = await _recover_completed_attempt(
            proposal, existing_attempt, principal_id, recovery_client,
        )
        if recovered is not None:
            return recovered

    if await is_kill_switch_active():
        raise RuntimeError("Emergency kill switch is active. Payment execution halted.")

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

    # Existing local orders are idempotent. Attempt rows cover the failure window
    # before a provider order ID is durably available.
    attempt = await get_paypal_attempt_by_proposal(proposal_id)
    if attempt is not None:
        if attempt.owner_user_id != principal_id or attempt.expected_amount != proposal.total \
                or attempt.expected_currency != proposal.currency:
            raise PaymentProposalNotApprovedError("Stored checkout attempt binding changed.")
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT consent_id, binding_hash FROM purchase_consents "
                "WHERE proposal_id = ? AND owner_user_id = ? ORDER BY consented_at DESC LIMIT 1",
                (proposal_id, principal_id),
            )
            consent = await cur.fetchone()
        if (consent is None or consent[0] != attempt.consent_id
                or consent[1] != attempt.consent_binding_hash):
            raise PaymentProposalNotApprovedError("Checkout consent binding is no longer current.")
    existing_order = await get_paypal_order_by_proposal_from_db(proposal_id)
    if existing_order is not None:
        if existing_order.status == PayPalOrderStatus.COMPLETED:
            raise PaymentOrderAlreadySettledError(
                f"Proposal '{proposal_id}' is already settled under PayPal order "
                f"'{existing_order.order_id}'."
            )
        if existing_order.status in (PayPalOrderStatus.CREATED, PayPalOrderStatus.APPROVED) \
                and attempt is not None \
                and attempt.status == OrderAttemptStatus.PROVIDER_CREATED:
            return PaymentOrderResponse(
                order_id=existing_order.order_id,
                proposal_id=existing_order.proposal_id,
                status=existing_order.status,
                currency=existing_order.currency,
                total_amount=existing_order.total_amount,
                approval_url=existing_order.approval_url,
                created_at=existing_order.created_at,
            )
        if attempt is None:
            raise PayPalOrderError(
                "A legacy checkout exists without a durable attempt record; recovery is required."
            )

    client = paypal_client or PayPalClient()
    requested_return_url = return_url or "http://localhost:5173/checkout/complete"
    requested_cancel_url = cancel_url or "http://localhost:5173/checkout/cancel"
    reconcile_existing = False
    if attempt is not None:
        if attempt.status == OrderAttemptStatus.PROVIDER_CREATED:
            order = await get_paypal_order_by_proposal_from_db(proposal_id)
            if order is not None:
                return PaymentOrderResponse(
                    order_id=order.order_id, proposal_id=order.proposal_id,
                    status=order.status, currency=order.currency,
                    total_amount=order.total_amount, approval_url=order.approval_url,
                    created_at=order.created_at,
                )
        if attempt.status in (OrderAttemptStatus.REQUESTING, OrderAttemptStatus.UNKNOWN,
                              OrderAttemptStatus.RECONCILIATION_REQUIRED):
            if attempt.provider_order_id:
                try:
                    provider_order = await client.get_order(attempt.provider_order_id)
                    _validate_provider_order(provider_order, proposal)
                    if provider_order.get("status") in (
                        "CREATED", "PAYER_ACTION_REQUIRED", "APPROVED"
                    ):
                        return await _persist_reconciled_provider_order(
                            attempt, proposal, mandate, principal_id, provider_order, conn=None
                        )
                except Exception:
                    pass
            if attempt.provider_order_id:
                raise PayPalOrderError(
                    "Provider order lookup was inconclusive; checkout remains under reconciliation."
                )
            if attempt.status == OrderAttemptStatus.REQUESTING \
                    and datetime.now(timezone.utc) - attempt.updated_at < ORDER_REQUEST_LEASE:
                raise PayPalOrderError(
                    "Checkout creation is already in progress; retry after reconciliation."
                )
            if datetime.now(timezone.utc) - attempt.created_at > timedelta(hours=6):
                async with get_connection() as conn:
                    await conn.execute("BEGIN IMMEDIATE")
                    changed = await transition_paypal_attempt(
                        attempt.attempt_id, attempt.status,
                        OrderAttemptStatus.RECONCILIATION_REQUIRED,
                        datetime.now(timezone.utc), db=conn,
                    )
                    if changed:
                        await AuditService.append_event(
                            event_type=AuditEventType.PAYMENT_ORDER_ATTEMPT_UNKNOWN,
                            entity_type=AuditEntityType.PAYMENT,
                            entity_id=attempt.attempt_id, actor=AuditActor.SYSTEM,
                            payload={"proposal_id": proposal_id,
                                     "attempt_id": attempt.attempt_id,
                                     "reason": "provider_idempotency_window_expired"},
                            db=conn,
                        )
                    await conn.commit()
                raise PayPalOrderError(
                    "Provider idempotency recovery window expired; manual reconciliation is required."
                )
            # PayPal's request ID is the provider idempotency key. Replaying this
            # exact persisted request is reconciliation, never a new-key retry.
            reconcile_existing = True
        if attempt.status != OrderAttemptStatus.PREPARED and not reconcile_existing:
            raise PayPalOrderError("This checkout attempt cannot be retried safely.")
    else:
        now = datetime.now(timezone.utc)
        async with get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                current_proposal = await get_proposal_from_db(proposal_id, db=conn)
                current_mandate = await get_mandate_from_db(mandate.mandate_id, db=conn)
                if current_proposal is None or current_mandate is None:
                    raise PaymentProposalNotApprovedError("Proposal authorization changed.")
                await require_current_purchase_consent(proposal_id, principal_id, db=conn)
                await require_current_assessment(
                    current_proposal, current_mandate, db=conn, require_human_review=True
                )
                cur = await conn.execute(
                    "SELECT consent_id, binding_hash FROM purchase_consents "
                    "WHERE proposal_id = ? AND owner_user_id = ? ORDER BY consented_at DESC LIMIT 1",
                    (proposal_id, principal_id),
                )
                consent = await cur.fetchone()
                if consent is None:
                    raise PaymentProposalNotApprovedError("Current purchase consent is required.")
                attempt = PayPalOrderAttempt(
                    attempt_id=f"pat_{uuid.uuid4().hex}", proposal_id=proposal_id,
                    owner_user_id=principal_id, consent_id=consent[0],
                    consent_binding_hash=consent[1], expected_amount=proposal.total,
                    expected_currency=proposal.currency,
                    provider_request_id=f"leash-{uuid.uuid4().hex}",
                    return_url=requested_return_url, cancel_url=requested_cancel_url,
                    status=OrderAttemptStatus.PREPARED, created_at=now, updated_at=now,
                )
                await insert_paypal_attempt(attempt, conn)
                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_ORDER_ATTEMPT_PREPARED,
                    entity_type=AuditEntityType.PAYMENT, entity_id=attempt.attempt_id,
                    actor=AuditActor.SYSTEM,
                    payload={"proposal_id": proposal_id, "attempt_id": attempt.attempt_id,
                             "amount": f"{proposal.total:.2f}", "currency": proposal.currency},
                    db=conn,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                # Another concurrent request may have won the UNIQUE proposal attempt.
                attempt = await get_paypal_attempt_by_proposal(proposal_id)
                if attempt is None:
                    raise

    # Revalidate against current persisted authorization immediately before the
    # provider call. Commit REQUESTING, then release SQLite before network I/O.
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if await is_kill_switch_active(db=conn):
                raise RuntimeError("Emergency kill switch is active. Payment halted.")
            current_proposal = await get_proposal_from_db(proposal_id, db=conn)
            current_mandate = await get_mandate_from_db(mandate.mandate_id, db=conn)
            if current_proposal is None or current_mandate is None:
                raise PaymentProposalNotApprovedError("Checkout authorization changed.")
            await require_current_purchase_consent(proposal_id, principal_id, db=conn)
            await require_current_assessment(
                current_proposal, current_mandate, db=conn, require_human_review=True
            )
            current_attempt = await get_paypal_attempt_by_proposal(proposal_id, db=conn)
            if current_attempt is None or current_attempt.attempt_id != attempt.attempt_id:
                raise PayPalOrderError("Durable checkout attempt changed.")
            if current_attempt.status in (OrderAttemptStatus.PREPARED, OrderAttemptStatus.UNKNOWN):
                changed = await transition_paypal_attempt(
                    attempt.attempt_id, current_attempt.status, OrderAttemptStatus.REQUESTING,
                    datetime.now(timezone.utc), db=conn,
                )
                if not changed:
                    raise PayPalOrderError("Checkout attempt is already in progress.")
            elif current_attempt.status == OrderAttemptStatus.REQUESTING:
                if datetime.now(timezone.utc) - current_attempt.updated_at < ORDER_REQUEST_LEASE:
                    raise PayPalOrderError(
                        "Checkout creation is already in progress; retry after reconciliation."
                    )
                changed = await transition_paypal_attempt(
                    attempt.attempt_id, OrderAttemptStatus.REQUESTING,
                    OrderAttemptStatus.REQUESTING, datetime.now(timezone.utc), db=conn,
                )
                if not changed:
                    raise PayPalOrderError("Checkout attempt is already in progress.")
            else:
                raise PayPalOrderError("Checkout attempt cannot be sent to PayPal.")
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    try:
        order_result = await client.create_order(
            proposal=proposal, mandate=mandate,
            paypal_request_id=attempt.provider_request_id,
            return_url=attempt.return_url, cancel_url=attempt.cancel_url,
        )
        _validate_provider_creation_response(order_result, proposal)
    except Exception as exc:
        async with get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await transition_paypal_attempt(
                attempt.attempt_id, OrderAttemptStatus.REQUESTING, OrderAttemptStatus.UNKNOWN,
                datetime.now(timezone.utc), db=conn,
            )
            await AuditService.append_event(
                event_type=AuditEventType.PAYMENT_ORDER_ATTEMPT_UNKNOWN,
                entity_type=AuditEntityType.PAYMENT, entity_id=attempt.attempt_id,
                actor=AuditActor.SYSTEM,
                payload={"proposal_id": proposal_id, "attempt_id": attempt.attempt_id,
                         "reason": "provider_outcome_uncertain"}, db=conn,
            )
            await conn.commit()
        if isinstance(exc, PayPalNetworkError):
            raise
        raise PayPalOrderError(
            "PayPal order result could not be verified; reconciliation is required."
        ) from exc

    paypal_order_id = order_result["order_id"]
    approval_url = order_result["approval_url"]
    order_status = PayPalOrderStatus(order_result.get("status", "CREATED"))
    # Anchor the provider result before any dependent local order work. If the
    # following transaction fails, recovery can fetch this exact provider order.
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        saved = await transition_paypal_attempt(
            attempt.attempt_id, OrderAttemptStatus.REQUESTING, OrderAttemptStatus.UNKNOWN,
            datetime.now(timezone.utc), provider_order_id=paypal_order_id,
            approval_url=approval_url, db=conn,
        )
        if not saved:
            await conn.rollback()
            raise PayPalOrderError("Provider order result could not be anchored for recovery.")
        await AuditService.append_event(
            event_type=AuditEventType.PAYMENT_ORDER_ATTEMPT_UNKNOWN,
            entity_type=AuditEntityType.PAYMENT, entity_id=attempt.attempt_id,
            actor=AuditActor.SYSTEM,
            payload={"proposal_id": proposal_id, "attempt_id": attempt.attempt_id,
                     "provider_order_id": paypal_order_id,
                     "reason": "awaiting_local_finalization"}, db=conn,
        )
        await conn.commit()

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
        paypal_request_id=attempt.provider_request_id,
        created_at=now,
        updated_at=now,
    )

    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            current_proposal = await get_proposal_from_db(proposal_id, db=conn)
            current_mandate = await get_mandate_from_db(mandate.mandate_id, db=conn)
            authorization_valid = bool(current_proposal and current_mandate)
            if authorization_valid:
                try:
                    if await is_kill_switch_active(db=conn):
                        authorization_valid = False
                    await require_current_purchase_consent(proposal_id, principal_id, db=conn)
                    await require_current_assessment(
                        current_proposal, current_mandate, db=conn, require_human_review=True
                    )
                except Exception:
                    authorization_valid = False
            if not authorization_valid:
                order_record = order_record.model_copy(update={"status": PayPalOrderStatus.FAILED})
            await insert_paypal_order_in_db(order_record, db=conn)
            attempt_status = (OrderAttemptStatus.PROVIDER_CREATED if authorization_valid
                              else OrderAttemptStatus.RECONCILIATION_REQUIRED)
            await transition_paypal_attempt(
                attempt.attempt_id, OrderAttemptStatus.UNKNOWN, attempt_status,
                datetime.now(timezone.utc), provider_order_id=paypal_order_id,
                approval_url=approval_url, db=conn,
            )
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

    if not authorization_valid:
        raise PaymentProposalNotApprovedError(
            "Authorization changed after checkout creation; the order is recorded and cannot be captured."
        )

    return PaymentOrderResponse(
        order_id=order_record.order_id,
        proposal_id=order_record.proposal_id,
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
    if order_record.status in (
        PayPalOrderStatus.CAPTURING, PayPalOrderStatus.RECONCILIATION_REQUIRED
    ):
        raise PayPalCaptureError(
            "Capture outcome is uncertain and requires explicit reconciliation before capture."
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
    if paypal_order_data.get("id") != order_id:
        raise PayPalOrderError("PayPal returned a different order than requested.")
    paypal_status = paypal_order_data.get("status")

    if paypal_status not in ("APPROVED", "COMPLETED"):
        raise PaymentOrderNotApprovedError(
            f"PayPal order '{order_id}' is not approved by buyer "
            f"(current PayPal status: '{paypal_status}')."
        )

    # Re-verify amount and currency from PayPal response against proposal
    purchase_units = paypal_order_data.get("purchase_units", [])
    if not isinstance(purchase_units, list) or len(purchase_units) != 1:
        raise ValueError(f"PayPal order '{order_id}' contains no purchase units.")

    if not isinstance(purchase_units[0], dict) or \
            purchase_units[0].get("reference_id") != proposal.proposal_id:
        raise ValueError("PayPal order reference does not match the proposal.")

    amount_obj = purchase_units[0].get("amount", {})
    currency_code = amount_obj.get("currency_code")
    amount_str = amount_obj.get("value")

    if currency_code != proposal.currency:
        raise ValueError(
            f"PayPal order currency '{currency_code}' does not match proposal "
            f"currency '{proposal.currency}'."
        )

    if order_record.status in (PayPalOrderStatus.CAPTURING,
                               PayPalOrderStatus.RECONCILIATION_REQUIRED) \
            and paypal_status != "COMPLETED":
        raise PayPalCaptureError(
            "Capture remains uncertain; PayPal has not confirmed a completed capture."
        )

    if Decimal(amount_str) != proposal.total:
        raise ValueError(
            f"PayPal order amount '{amount_str}' does not match proposal total '{proposal.total}'."
        )

    capture_req_id = f"cap_req_{order_id}"
    capture_id: str
    now_utc = datetime.now(timezone.utc)
    raw_capture = paypal_order_data

    if paypal_status == "APPROVED":
        # Reserve exactly one capture attempt and revalidate current authority,
        # then commit before network I/O. CAPTURING survives process crashes.
        async with get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                if await is_kill_switch_active(db=conn):
                    raise RuntimeError("Emergency kill switch is active. Payment execution halted.")
                current_order = await get_paypal_order_from_db(order_id, db=conn)
                current_proposal = await get_proposal_from_db(proposal.proposal_id, db=conn)
                current_mandate = await get_mandate_from_db(mandate.mandate_id, db=conn)
                if current_order is None or current_order.owner_user_id != principal_id:
                    raise PaymentOrderNotFoundError("Payment order authorization changed.")
                if current_order.status == PayPalOrderStatus.COMPLETED and current_order.capture_id:
                    return PaymentCaptureResponse(
                        order_id=current_order.order_id, proposal_id=current_order.proposal_id,
                        mandate_id=current_order.mandate_id, capture_id=current_order.capture_id,
                        status=PayPalOrderStatus.COMPLETED, currency=current_order.currency,
                        total_amount=current_order.total_amount,
                        captured_at=current_order.captured_at or current_order.updated_at,
                        receipt={"order_id": current_order.order_id,
                                 "capture_id": current_order.capture_id,
                                 "status": "COMPLETED", "amount": str(current_order.total_amount),
                                 "currency": current_order.currency},
                    )
                if current_order.status in (PayPalOrderStatus.RECONCILIATION_REQUIRED,
                                             PayPalOrderStatus.CAPTURING):
                    await conn.rollback()
                    raise PayPalCaptureError(
                        "Capture is already in progress or requires explicit reconciliation."
                    )
                if current_order.status not in (
                    PayPalOrderStatus.CREATED, PayPalOrderStatus.PAYER_ACTION_REQUIRED,
                    PayPalOrderStatus.APPROVED,
                ):
                    raise PaymentOrderNotApprovedError("Order is not capturable.")
                if current_proposal is None or current_mandate is None:
                    raise PaymentProposalNotApprovedError("Authorization changed before capture.")
                if current_proposal.status != ProposalStatus.APPROVED:
                    raise PaymentProposalNotApprovedError("Proposal is not currently approved.")
                if current_mandate.status != MandateStatus.ACTIVE or \
                        current_mandate.owner_user_id != principal_id or \
                        current_mandate.currency != current_proposal.currency or \
                        datetime.now(timezone.utc) >= current_mandate.expires_at:
                    raise PaymentMandateInactiveError("Mandate authorization is no longer current.")
                await require_current_purchase_consent(
                    current_proposal.proposal_id, principal_id, db=conn
                )
                from app.services.intent_audit_service import require_current_assessment
                await require_current_assessment(
                    current_proposal, current_mandate, db=conn, require_human_review=True
                )
                current_spend = await get_cumulative_mandate_spent(
                    current_mandate.mandate_id,
                    exclude_proposal_id=current_proposal.proposal_id, db=conn,
                )
                if current_spend + current_proposal.total > current_mandate.max_total_budget:
                    raise PaymentBudgetExceededError("Current mandate budget is insufficient.")
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
                    raise PaymentBudgetExceededError("Current mandate item limits are exceeded.")
                if (
                    current_mandate.allowed_merchants
                    and current_proposal.merchant not in current_mandate.allowed_merchants
                ) or (
                    current_mandate.allowed_categories
                    and current_proposal.category not in current_mandate.allowed_categories
                ):
                    raise PaymentBudgetExceededError("Current mandate allowlists exclude proposal.")
                changed = await update_paypal_order_status_in_db(
                    order_id, PayPalOrderStatus.CAPTURING,
                    datetime.now(timezone.utc).isoformat(), db=conn,
                )
                if not changed:
                    raise PayPalCaptureError("Another capture attempt is already in progress.")
                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_CAPTURE_ATTEMPTED,
                    entity_type=AuditEntityType.PAYMENT, entity_id=order_id,
                    actor=AuditActor.SYSTEM,
                    payload={"order_id": order_id,
                             "proposal_id": current_proposal.proposal_id,
                             "capture_request_id": capture_req_id,
                             "outcome": "REQUESTING"}, db=conn,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        try:
            capture_result = await client.capture_order(order_id, capture_req_id)
            raw_capture = capture_result.get("raw_response", capture_result)
        except (PayPalNetworkError, PayPalCaptureError) as exc:
            try:
                raw_capture = await client.get_order(order_id)
            except Exception:
                raw_capture = {}
            if not isinstance(raw_capture, dict) or raw_capture.get("status") != "COMPLETED":
                async with get_connection() as conn:
                    await conn.execute("BEGIN IMMEDIATE")
                    changed = await update_paypal_order_status_in_db(
                        order_id, PayPalOrderStatus.RECONCILIATION_REQUIRED,
                        datetime.now(timezone.utc).isoformat(), db=conn,
                    )
                    if changed:
                        await AuditService.append_event(
                            event_type=AuditEventType.PAYMENT_FAILED,
                            entity_type=AuditEntityType.PAYMENT, entity_id=order_id,
                            actor=AuditActor.SYSTEM,
                            payload={"order_id": order_id, "proposal_id": proposal.proposal_id,
                                     "outcome": "RECONCILIATION_REQUIRED"}, db=conn,
                        )
                    else:
                        latest = await get_paypal_order_from_db(order_id, db=conn)
                        if latest and latest.status == PayPalOrderStatus.COMPLETED \
                                and latest.capture_id:
                            await conn.commit()
                            return PaymentCaptureResponse(
                                order_id=latest.order_id, proposal_id=latest.proposal_id,
                                mandate_id=latest.mandate_id, capture_id=latest.capture_id,
                                status=PayPalOrderStatus.COMPLETED,
                                currency=latest.currency, total_amount=latest.total_amount,
                                captured_at=latest.captured_at or latest.updated_at,
                                receipt={"order_id": latest.order_id,
                                         "capture_id": latest.capture_id,
                                         "status": "COMPLETED",
                                         "amount": str(latest.total_amount),
                                         "currency": latest.currency},
                            )
                    await conn.commit()
                raise PayPalCaptureError(
                    "Capture outcome is uncertain; order requires reconciliation."
                ) from exc

    if not isinstance(raw_capture, dict):
        raise PayPalCaptureError("PayPal capture response is malformed.")
    try:
        if raw_capture.get("id") != order_id:
            raise PayPalCaptureError("PayPal capture response belongs to a different order.")
        _validate_provider_binding(
            raw_capture, proposal.proposal_id, proposal.currency, proposal.total,
        )
        capture_id, now_utc = _confirmed_capture(raw_capture, proposal.currency, proposal.total)
    except PayPalCaptureError:
        await _mark_capture_reconciliation_required(
            order_id, proposal.proposal_id, "provider_capture_evidence_invalid",
        )
        raise
    now_iso = now_utc.isoformat()

    # Persist provider-confirmed truth atomically; authorization was checked
    # immediately before dispatch and must not erase a real completed payment.
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            current_order = await get_paypal_order_from_db(order_id, db=conn)
            if current_order is None:
                raise PaymentOrderNotFoundError("Order disappeared during capture finalization.")
            if current_order.status == PayPalOrderStatus.COMPLETED and current_order.capture_id:
                await conn.commit()
                capture_id = current_order.capture_id
                now_utc = current_order.captured_at or current_order.updated_at
            else:
                updated = await update_paypal_order_captured_in_db(
                    order_id, capture_id, capture_req_id, now_iso, now_iso, db=conn
                )
                if not updated:
                    raise RuntimeError("Payment order changed during capture finalization.")
                proposal_updated = await update_proposal_status_in_db(
                    proposal_id=proposal.proposal_id, new_status=ProposalStatus.SETTLED,
                    expected_current_status=ProposalStatus.APPROVED, db=conn,
                )
                if not proposal_updated:
                    raise RuntimeError("Proposal changed during capture finalization.")
                await AuditService.append_event(
                    event_type=AuditEventType.PAYMENT_CAPTURED,
                    entity_type=AuditEntityType.PAYMENT, entity_id=order_id,
                    actor=AuditActor.SYSTEM,
                    payload={"order_id": order_id, "capture_id": capture_id,
                             "proposal_id": proposal.proposal_id,
                             "mandate_id": mandate.mandate_id,
                             "total": str(proposal.total), "currency": proposal.currency,
                             "principal_id": principal_id}, db=conn,
                )
                await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    return PaymentCaptureResponse(
        order_id=order_id, proposal_id=proposal.proposal_id, mandate_id=mandate.mandate_id,
        capture_id=capture_id, status=PayPalOrderStatus.COMPLETED,
        currency=proposal.currency, total_amount=proposal.total, captured_at=now_utc,
        receipt={"order_id": order_id, "capture_id": capture_id, "status": "COMPLETED",
                 "amount": str(proposal.total), "currency": proposal.currency},
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
        status=order_record.status,
        currency=order_record.currency,
        total_amount=order_record.total_amount,
        approval_url=order_record.approval_url,
        created_at=order_record.created_at,
    )


async def get_payment_checkout_state(
    proposal_id: str, principal_id: str,
):
    """Read current owner's persisted checkout state for safe browser reload recovery."""
    from app.schemas.payment import PaymentCheckoutState
    proposal = await get_proposal_from_db(proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(f"Proposal '{proposal_id}' not found.")
    mandate = await get_mandate_from_db(proposal.mandate_id)
    if mandate is None or mandate.owner_user_id != principal_id:
        raise MandateOwnershipError("The current session does not own this checkout.")
    order = await get_paypal_order_by_proposal_from_db(proposal_id)
    if order is not None and order.status == PayPalOrderStatus.COMPLETED \
            and order.capture_id:
        return PaymentCheckoutState(
            proposal_id=proposal_id, status=order.status, consent_recorded=True,
            order_id=order.order_id, approval_url=order.approval_url,
            currency=order.currency, total_amount=order.total_amount,
            payment_status=PayPalOrderStatus.COMPLETED,
        )
    if order is not None and order.status == PayPalOrderStatus.COMPLETED:
        return PaymentCheckoutState(
            proposal_id=proposal_id, status=PayPalOrderStatus.RECONCILIATION_REQUIRED,
            consent_recorded=False, order_id=None, approval_url=None,
            currency=order.currency, total_amount=order.total_amount,
            payment_status=PayPalOrderStatus.RECONCILIATION_REQUIRED,
            recovery_available=True,
        )
    try:
        await require_current_purchase_consent(proposal_id, principal_id)
        has_consent = True
    except Exception as exc:
        from app.services.purchase_consent_service import PurchaseConsentDeniedError
        if not isinstance(exc, PurchaseConsentDeniedError):
            raise
        has_consent = False
    if not has_consent:
        attempt = await get_paypal_attempt_by_proposal(proposal_id)
        if attempt is not None and attempt.owner_user_id != principal_id:
            raise MandateOwnershipError("The current session does not own this checkout.")
        unresolved_status = (
            order.status if order is not None else attempt.status if attempt is not None else None
        )
        return PaymentCheckoutState(
            proposal_id=proposal_id, status=unresolved_status, consent_recorded=False,
            order_id=None, approval_url=None, currency=proposal.currency,
            total_amount=proposal.total,
            payment_status=order.status if order is not None else None,
            recovery_available=bool(
                order is not None or (attempt is not None and attempt.provider_order_id)
            ),
        )
    order = order if has_consent else None
    attempt = await get_paypal_attempt_by_proposal(proposal_id) if has_consent else None
    if order is not None:
        return PaymentCheckoutState(
            proposal_id=proposal_id, status=order.status, consent_recorded=True,
            order_id=order.order_id,
            approval_url=order.approval_url, currency=order.currency,
            total_amount=order.total_amount, payment_status=order.status,
            recovery_available=True,
        )
    if attempt is None:
        return PaymentCheckoutState(
            proposal_id=proposal_id, status=None, consent_recorded=has_consent,
            order_id=None, approval_url=None, currency=proposal.currency,
            total_amount=proposal.total, payment_status=None,
        )
    if attempt.owner_user_id != principal_id:
        raise MandateOwnershipError("The current session does not own this checkout.")
    return PaymentCheckoutState(
        proposal_id=proposal_id, status=attempt.status, consent_recorded=True,
        order_id=attempt.provider_order_id, approval_url=attempt.approval_url,
        currency=attempt.expected_currency, total_amount=attempt.expected_amount,
        recovery_available=bool(attempt.provider_order_id),
    )


async def recover_proposal_checkout(
    proposal_id: str,
    principal_id: str,
    paypal_client: PayPalClient | None = None,
) -> PaymentRecoveryResponse:
    """Verify a persisted PayPal order for this owner without creating/capturing it.

    This is a factual reconciliation operation. It deliberately does not require
    current consent or mandate validity to record a provider-confirmed completed
    payment. Non-completed outcomes never authorize capture.
    """
    proposal = await get_proposal_from_db(proposal_id)
    if proposal is None:
        raise ProposalNotFoundError("Proposal not found.")
    mandate = await get_mandate_from_db(proposal.mandate_id)
    if mandate is None or mandate.owner_user_id != principal_id:
        raise MandateOwnershipError("The current session does not own this checkout.")

    attempt = await get_paypal_attempt_by_proposal(proposal_id)
    order = await get_paypal_order_by_proposal_from_db(proposal_id)
    if attempt is not None and attempt.owner_user_id != principal_id:
        raise MandateOwnershipError("The current session does not own this checkout.")
    if order is not None and order.owner_user_id != principal_id:
        raise MandateOwnershipError("The current session does not own this checkout.")
    if (attempt is not None and order is not None and attempt.provider_order_id
            and attempt.provider_order_id != order.order_id):
        await _mark_capture_reconciliation_required(
            order.order_id, proposal_id, "local_attempt_order_binding_mismatch",
        )
        raise PayPalOrderError("Stored checkout order binding requires reconciliation.")
    provider_order_id = order.order_id if order is not None else (
        attempt.provider_order_id if attempt is not None else None
    )
    if not provider_order_id or (order is None and attempt is None):
        raise PaymentOrderNotFoundError("No persisted provider order is available to verify.")

    client = paypal_client or PayPalClient()
    provider = await client.get_order(provider_order_id)
    if not isinstance(provider, dict) or provider.get("id") != provider_order_id:
        if order is not None:
            await _mark_capture_reconciliation_required(
                provider_order_id, proposal_id, "provider_order_identity_mismatch",
            )
        elif attempt is not None:
            await _mark_attempt_reconciliation_required(
                attempt, "provider_order_identity_mismatch",
            )
        raise PayPalOrderError("PayPal order identity could not be verified.")

    provider_status = provider.get("status")
    if provider_status not in ("CREATED", "PAYER_ACTION_REQUIRED", "APPROVED", "COMPLETED"):
        if order is not None:
            await _mark_capture_reconciliation_required(
                provider_order_id, proposal_id, "provider_order_status_unresolved",
            )
            current_status = PayPalOrderStatus.RECONCILIATION_REQUIRED
        else:
            await _mark_attempt_reconciliation_required(
                attempt, "provider_order_status_unresolved",
            )
            current_status = OrderAttemptStatus.RECONCILIATION_REQUIRED
        return PaymentRecoveryResponse(
            proposal_id=proposal_id, status=current_status,
            provider_status=None, payment_completed=False,
        )

    expected_amount = order.total_amount if order is not None else attempt.expected_amount
    expected_currency = order.currency if order is not None else attempt.expected_currency
    try:
        if provider_status == "COMPLETED":
            _validate_provider_binding(
                provider, proposal_id, expected_currency, expected_amount,
            )
            _confirmed_capture(provider, expected_currency, expected_amount)
            if order is None:
                recovered = await _recover_completed_attempt(
                    proposal, attempt, principal_id, client, provider_order=provider,
                )
                if recovered is None:
                    raise PayPalCaptureError("Completed provider payment could not be recorded.")
                final_status = PayPalOrderStatus.COMPLETED
            else:
                captured = await _reconcile_completed_capture(order, provider, principal_id)
                final_status = captured.status
            return PaymentRecoveryResponse(
                proposal_id=proposal_id, status=final_status,
                provider_status="COMPLETED", payment_completed=True,
            )

        units = provider.get("purchase_units")
        if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], dict):
            raise PayPalOrderError("PayPal order evidence is incomplete.")
        amount = units[0].get("amount")
        if (units[0].get("reference_id") != proposal_id or not isinstance(amount, dict)
                or amount.get("currency_code") != expected_currency
                or Decimal(str(amount.get("value"))) != expected_amount):
            raise PayPalOrderError("PayPal order does not match its persisted checkout attempt.")
    except (PayPalCaptureError, PayPalOrderError, ValueError):
        if order is not None:
            await _mark_capture_reconciliation_required(
                provider_order_id, proposal_id, "provider_order_evidence_mismatch",
            )
            current_status = PayPalOrderStatus.RECONCILIATION_REQUIRED
        else:
            await _mark_attempt_reconciliation_required(
                attempt, "provider_order_evidence_mismatch",
            )
            current_status = OrderAttemptStatus.RECONCILIATION_REQUIRED
        return PaymentRecoveryResponse(
            proposal_id=proposal_id, status=current_status,
            provider_status=provider_status, payment_completed=False,
        )

    # A non-completed provider state is only an observation. Do not transition it
    # into an actionable capture state through this recovery-only endpoint.
    current_status = order.status if order is not None else attempt.status
    return PaymentRecoveryResponse(
        proposal_id=proposal_id, status=current_status,
        provider_status=provider_status, payment_completed=False,
    )


async def _mark_attempt_reconciliation_required(
    attempt: PayPalOrderAttempt, reason: str,
) -> None:
    """Mark an attempt uncertain with one atomic, chained audit event."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            current = await get_paypal_attempt_by_proposal(attempt.proposal_id, db=conn)
            if current is not None and current.status != OrderAttemptStatus.RECONCILIATION_REQUIRED:
                changed = await transition_paypal_attempt(
                    current.attempt_id, current.status,
                    OrderAttemptStatus.RECONCILIATION_REQUIRED,
                    datetime.now(timezone.utc), db=conn,
                )
                if changed:
                    await AuditService.append_event(
                        event_type=AuditEventType.PAYMENT_ORDER_ATTEMPT_UNKNOWN,
                        entity_type=AuditEntityType.PAYMENT,
                        entity_id=current.attempt_id, actor=AuditActor.SYSTEM,
                        payload={"proposal_id": current.proposal_id,
                                 "attempt_id": current.attempt_id, "reason": reason}, db=conn,
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def verify_payment_return(proposal_id: str, order_id: str, principal_id: str,
                                paypal_client: PayPalClient | None = None):
    """Treat redirect identifiers as untrusted and verify order facts with PayPal."""
    from app.schemas.payment import PaymentReturnResponse
    order = await get_paypal_order_from_db(order_id)
    if order is None or order.proposal_id != proposal_id:
        raise PaymentOrderNotFoundError("Checkout return does not match a local order.")
    if order.owner_user_id != principal_id:
        raise MandateOwnershipError("The current session does not own this checkout.")
    client = paypal_client or PayPalClient()
    provider = await client.get_order(order_id)
    provider_id = provider.get("id")
    if provider_id != order_id:
        if order.status != PayPalOrderStatus.COMPLETED:
            await _mark_capture_reconciliation_required(
                order_id, proposal_id, "provider_order_identity_mismatch",
            )
        raise PayPalOrderError("PayPal returned a different order than requested.")
    status = provider.get("status")
    if status not in ("APPROVED", "CREATED", "PAYER_ACTION_REQUIRED", "COMPLETED"):
        raise PaymentOrderNotApprovedError("PayPal has not confirmed buyer approval.")
    if status == "COMPLETED":
        captured = await _reconcile_completed_capture(
            order, provider, principal_id,
        )
        return PaymentReturnResponse(
            proposal_id=proposal_id, order_id=order_id, status=captured.status,
            payment_completed=True,
        )
    proposal = await get_proposal_from_db(proposal_id)
    if proposal is None:
        raise ProposalNotFoundError("Checkout proposal is no longer available.")
    _validate_provider_order(provider, proposal)
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read under the write lock. The earlier snapshot may predate a
            # concurrent capture reservation or finalization.
            current_order = await get_paypal_order_from_db(order_id, db=conn)
            if current_order is None:
                raise PaymentOrderNotFoundError("Checkout order disappeared during verification.")
            if current_order.status == PayPalOrderStatus.COMPLETED and current_order.capture_id:
                await conn.commit()
                return PaymentReturnResponse(
                    proposal_id=proposal_id, order_id=order_id,
                    status=PayPalOrderStatus.COMPLETED, payment_completed=True,
                )
            mapped = current_order.status
            if current_order.status == PayPalOrderStatus.COMPLETED:
                mapped = PayPalOrderStatus.RECONCILIATION_REQUIRED
            elif current_order.status in (
                PayPalOrderStatus.CREATED, PayPalOrderStatus.PAYER_ACTION_REQUIRED
            ):
                mapped = (PayPalOrderStatus.APPROVED if status == "APPROVED"
                          else PayPalOrderStatus.PAYER_ACTION_REQUIRED
                          if status == "PAYER_ACTION_REQUIRED" else current_order.status)
            elif current_order.status == PayPalOrderStatus.RECONCILIATION_REQUIRED \
                    and status == "APPROVED":
                mapped = PayPalOrderStatus.APPROVED
            # Never let a return check reopen an active capture. Uncertain state
            # can return to APPROVED only after this provider lookup says APPROVED.
            if current_order.status == PayPalOrderStatus.CAPTURING:
                mapped = PayPalOrderStatus.CAPTURING
            if mapped != current_order.status:
                if current_order.status == PayPalOrderStatus.COMPLETED \
                        and not current_order.capture_id:
                    cursor = await conn.execute(
                        "UPDATE paypal_orders SET status=?, updated_at=? "
                        "WHERE order_id=? AND status=? AND capture_id IS NULL",
                        (mapped.value, datetime.now(timezone.utc).isoformat(), order_id,
                         PayPalOrderStatus.COMPLETED.value),
                    )
                    changed = cursor.rowcount == 1
                else:
                    changed = await update_paypal_order_status_in_db(
                        order_id, mapped, datetime.now(timezone.utc).isoformat(), db=conn
                    )
                if changed:
                    await AuditService.append_event(
                        event_type=AuditEventType.PAYMENT_APPROVAL_OBSERVED,
                        entity_type=AuditEntityType.PAYMENT, entity_id=order_id,
                        actor=AuditActor.USER,
                        payload={"order_id": order_id, "proposal_id": proposal_id,
                                 "provider_status": status}, db=conn,
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return PaymentReturnResponse(
        proposal_id=proposal_id, order_id=order_id, status=mapped,
        payment_completed=False,
    )
