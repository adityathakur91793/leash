"""Owner-scoped read projection for dashboard activity and payment summaries."""

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import aiosqlite

from app.db.database import get_connection
from app.schemas.activity import (
    ActivityEvent,
    ActivityOverviewResponse,
    MandateStatusCounts,
    PaymentStatusCounts,
    ProposalStatusCounts,
    SettlementTotal,
    UnresolvedCheckoutAttemptCounts,
)
from app.services.audit_service import AuditService

_EVENT_SUMMARIES = {
    "MANDATE_CREATED": "Mandate created",
    "MANDATE_CONFIRMED": "Mandate confirmed",
    "MANDATE_SIGNED": "Mandate signed",
    "MANDATE_ACTIVATED": "Mandate activated",
    "MANDATE_REVOKED": "Mandate revoked",
    "MANDATE_EXPIRED": "Mandate expired",
    "PROPOSAL_CREATED": "Purchase proposal submitted",
    "PROPOSAL_EVALUATED": "Proposal evaluated by policy",
    "PROPOSAL_APPROVED": "Purchase consent recorded",
    "PURCHASE_CONSENT_RECORDED": "Purchase consent recorded",
    "PROPOSAL_REJECTED": "Proposal rejected",
    "EXCEPTION_APPROVED": "Exception review recorded",
    "REPLAY_ATTEMPT_BLOCKED": "Replay attempt blocked",
    "AUTHORIZATION_BLOCKED": "Authorization blocked",
    "PAYMENT_ORDER_CREATED": "PayPal checkout order created",
    "PAYMENT_ORDER_ATTEMPT_PREPARED": "PayPal checkout prepared",
    "PAYMENT_ORDER_ATTEMPT_UNKNOWN": "PayPal checkout needs reconciliation",
    "PAYMENT_ORDER_ATTEMPT_RECONCILED": "PayPal checkout reconciled",
    "PAYMENT_APPROVAL_OBSERVED": "PayPal approval observed",
    "PAYMENT_CAPTURE_ATTEMPTED": "Payment capture requested",
    "PAYMENT_CAPTURED": "Payment settlement confirmed",
    "PAYMENT_FAILED": "Payment needs attention",
    "PAYMENT_RECONCILED": "Payment settlement reconciled",
    "INTENT_ASSESSMENT_COMPLETED": "Intent Auditor assessment completed",
    "INTENT_ASSESSMENT_FAILED": "Intent Auditor assessment unavailable",
}

_ACTORS = {"USER", "AGENT", "SYSTEM", "ADMIN", "POLICY_ENGINE"}
_PAYMENT_STATUSES = (
    "CREATED", "PAYER_ACTION_REQUIRED", "APPROVED", "CAPTURING", "COMPLETED",
    "FAILED", "CANCELLED", "RECONCILIATION_REQUIRED",
)
_MANDATE_STATUSES = ("DRAFT", "CONFIRMED", "SIGNED", "ACTIVE", "EXPIRED", "REVOKED")
_PROPOSAL_STATUSES = (
    "SUBMITTED", "EVALUATED", "APPROVED", "SETTLED", "REJECTED", "WITHDRAWN",
)


@asynccontextmanager
async def _read_transaction(conn: aiosqlite.Connection):
    """Give related dashboard reads one snapshot and close it on every path."""
    await conn.execute("BEGIN")
    try:
        yield
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def get_activity_overview(owner_user_id: str) -> ActivityOverviewResponse:
    """Return a sanitized view containing only entities owned by the session principal."""
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        # The integrity verifier uses a separate connection after this read snapshot.
        async with _read_transaction(conn):
            snapshot = await _get_activity_snapshot(owner_user_id, conn)
    integrity = await AuditService.verify_chain()
    return ActivityOverviewResponse(
        **snapshot,
        hash_chain_valid=integrity.valid,
        integrity_note=(
            "This checks audit-log hash-chain integrity only; it does not prove that "
            "underlying business events were correct."
        ),
    )


async def _get_activity_snapshot(
    owner_user_id: str, conn: aiosqlite.Connection
) -> dict[str, object]:
    """Build the response using one transaction for every activity aggregate."""
    cte = """
        WITH owned_entities AS (
            SELECT 'MANDATE' AS entity_type, m.mandate_id AS entity_id,
                   'Mandate' AS display_name, NULL AS amount, m.currency AS currency
            FROM mandates m WHERE m.owner_user_id = ?
            UNION ALL
            SELECT 'PROPOSAL', p.proposal_id, p.product_name, p.total, p.currency
            FROM proposals p JOIN mandates m ON m.mandate_id = p.mandate_id
            WHERE m.owner_user_id = ?
            UNION ALL
            SELECT 'PAYMENT', o.order_id, p.product_name, o.total_amount, o.currency
            FROM paypal_orders o
            JOIN proposals p ON p.proposal_id = o.proposal_id
            JOIN mandates m ON m.mandate_id = p.mandate_id
            WHERE m.owner_user_id = ? AND o.owner_user_id = ?
            UNION ALL
            SELECT 'PAYMENT', a.attempt_id, p.product_name,
                   a.expected_amount, a.expected_currency
            FROM paypal_order_attempts a
            JOIN proposals p ON p.proposal_id = a.proposal_id
            JOIN mandates m ON m.mandate_id = p.mandate_id
            WHERE m.owner_user_id = ? AND a.owner_user_id = ?
        )
    """
    count_query = cte + """
        SELECT COUNT(*) FROM audit_logs a
        JOIN owned_entities e ON e.entity_type = a.entity_type
                             AND e.entity_id = a.entity_id
    """
    async with conn.execute(count_query, (owner_user_id,) * 6) as cursor:
        total = int((await cursor.fetchone())[0])

    items_query = cte + """
        SELECT a.timestamp, a.event_type, a.entity_type,
               a.actor, e.display_name, e.amount, e.currency
        FROM audit_logs a
        JOIN owned_entities e ON e.entity_type = a.entity_type
                             AND e.entity_id = a.entity_id
        ORDER BY a.sequence_number DESC
        LIMIT 100
    """
    async with conn.execute(items_query, (owner_user_id,) * 6) as cursor:
        rows = await cursor.fetchall()

    mandate_counts = {state: 0 for state in _MANDATE_STATUSES}
    async with conn.execute(
        "SELECT status, expires_at FROM mandates WHERE owner_user_id = ?",
        (owner_user_id,),
    ) as cursor:
        now = datetime.now(timezone.utc)
        for row in await cursor.fetchall():
            state = row["status"]
            if state not in {"EXPIRED", "REVOKED"}:
                try:
                    expires_at = datetime.fromisoformat(
                        row["expires_at"].replace("Z", "+00:00")
                    )
                    if expires_at.tzinfo is None or now >= expires_at:
                        state = "EXPIRED"
                except (TypeError, ValueError):
                    state = "EXPIRED"
            if state in mandate_counts:
                mandate_counts[state] += 1

    proposal_counts = {state: 0 for state in _PROPOSAL_STATUSES}
    proposal_count_query = """
        SELECT p.status, COUNT(*) AS count
        FROM proposals p JOIN mandates m ON m.mandate_id = p.mandate_id
        WHERE m.owner_user_id = ? GROUP BY p.status
    """
    async with conn.execute(proposal_count_query, (owner_user_id,)) as cursor:
        for row in await cursor.fetchall():
            if row["status"] in proposal_counts:
                proposal_counts[row["status"]] = int(row["count"])

    payment_counts = {state: 0 for state in _PAYMENT_STATUSES}
    payment_query = """
        SELECT o.status, COUNT(*) AS count
        FROM paypal_orders o
        JOIN proposals p ON p.proposal_id = o.proposal_id
        JOIN mandates m ON m.mandate_id = p.mandate_id
        WHERE m.owner_user_id = ? AND o.owner_user_id = ?
        GROUP BY o.status
    """
    async with conn.execute(payment_query, (owner_user_id, owner_user_id)) as cursor:
        for row in await cursor.fetchall():
            if row["status"] in payment_counts:
                payment_counts[row["status"]] = int(row["count"])

    settlement_query = """
        SELECT o.total_amount, o.currency
        FROM paypal_orders o
        JOIN proposals p ON p.proposal_id = o.proposal_id
        JOIN mandates m ON m.mandate_id = p.mandate_id
        WHERE m.owner_user_id = ? AND o.owner_user_id = ?
          AND o.status = 'COMPLETED'
          AND o.capture_id IS NOT NULL
    """
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    confirmed_settlement_count = 0
    async with conn.execute(
        settlement_query, (owner_user_id, owner_user_id),
    ) as cursor:
        for row in await cursor.fetchall():
            confirmed_settlement_count += 1
            totals[row["currency"]] += Decimal(row["total_amount"])

    attempt_counts = {
        "UNKNOWN": 0,
        "PROVIDER_CREATED": 0,
        "RECONCILIATION_REQUIRED": 0,
    }
    attempt_query = """
        SELECT a.status, COUNT(*) AS count
        FROM paypal_order_attempts a
        JOIN proposals p ON p.proposal_id = a.proposal_id
        JOIN mandates m ON m.mandate_id = p.mandate_id
        WHERE m.owner_user_id = ? AND a.owner_user_id = ?
          AND a.status IN ('UNKNOWN', 'PROVIDER_CREATED', 'RECONCILIATION_REQUIRED')
          AND NOT EXISTS (
              SELECT 1 FROM paypal_orders o
              WHERE o.proposal_id = a.proposal_id AND o.owner_user_id = a.owner_user_id
          )
        GROUP BY a.status
    """
    async with conn.execute(attempt_query, (owner_user_id, owner_user_id)) as cursor:
        for row in await cursor.fetchall():
            attempt_counts[row["status"]] = int(row["count"])
    unresolved_attempt_count = sum(attempt_counts.values())

    events = [
        ActivityEvent(
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            entity_type=row["entity_type"],
            actor=row["actor"] if row["actor"] in _ACTORS else "SYSTEM",
            summary=_EVENT_SUMMARIES.get(row["event_type"], "Audit event recorded"),
            display_name=row["display_name"],
            amount=row["amount"],
            currency=row["currency"],
        )
        for row in rows
    ]
    return {
        "items": events,
        "total": total,
        "has_more": total > len(events),
        "mandate_status_counts": MandateStatusCounts(**mandate_counts),
        "proposal_status_counts": ProposalStatusCounts(**proposal_counts),
        "payment_status_counts": PaymentStatusCounts(**payment_counts),
        "unresolved_checkout_attempt_count": unresolved_attempt_count,
        "unresolved_checkout_attempt_status_counts": UnresolvedCheckoutAttemptCounts(
            **attempt_counts
        ),
        "confirmed_settlement_count": confirmed_settlement_count,
        "confirmed_settlement_totals": [
            SettlementTotal(currency=currency, amount=f"{amount:.2f}")
            for currency, amount in sorted(totals.items())
        ],
    }
