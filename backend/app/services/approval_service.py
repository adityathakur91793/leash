"""One-time exception approval token service.

Handles issuing and consuming short-lived HMAC-SHA256 approval tokens for
proposals that require user approval (soft-approval cases only).

Hard BLOCK rules (P01, P02, P03, P08, P11, P12) cannot be overridden by
any approval token.
All token registration, consumption, state transitions, and audit records
are committed atomically in single SQLite transactions under BEGIN IMMEDIATE.
"""

from datetime import datetime, timedelta, timezone
import logging
import secrets

from app.core.config import settings
from app.db.database import (
    get_connection,
    get_cumulative_mandate_spent,
    get_proposal_by_fingerprint,
    get_proposal_by_idempotency_key,
    get_today_reserved_spend,
)
from app.schemas.audit import AuditActor, AuditEntityType, AuditEventType
from app.schemas.policy import DecisionOutcome, EvaluationContext, RuleStatus
from app.schemas.proposal import ProposalStatus
from app.services.audit_service import AuditService
from app.services.kill_switch_service import enforce_kill_switch_guard
from app.services.mandate_service import MandateNotFoundError, get_mandate
from app.services.policy_engine import (
    evaluate_policy,
    generate_approval_token,
    register_approval_token,
    verify_and_consume_approval_token,
)
from app.services.proposal_service import ProposalNotFoundError, get_proposal

logger = logging.getLogger(__name__)

# Hard BLOCK rule IDs that cannot be overridden by any approval token
HARD_BLOCK_RULES: frozenset[str] = frozenset({"P01", "P02", "P03", "P08", "P11", "P12"})

_TOKEN_TTL_SECONDS = 900  # 15 minutes


async def issue_approval_token(
    proposal_id: str,
    user_session_id: str,
) -> dict:
    """Issue a short-lived HMAC-SHA256 approval token for a soft-approval proposal.

    Args:
        proposal_id: The proposal to bind the token to.
        user_session_id: The authenticated user session identifier.

    Returns:
        dict with keys: proposal_id, issued_at, expires_at, nonce, token.

    Raises:
        ValueError: If signing key is not configured, proposal is hard-blocked,
                    or proposal does not exist.
        MandateNotFoundError: If the associated mandate cannot be found.
    """
    if not settings.MANDATE_SIGNING_KEY:
        raise ValueError("Signing key not configured.")

    proposal = await get_proposal(proposal_id)
    mandate = await get_mandate(proposal.mandate_id)

    now = datetime.now(timezone.utc)
    today_iso = now.date().isoformat()

    async with get_connection() as db:
        today_reserved = await get_today_reserved_spend(
            mandate.mandate_id,
            today_iso,
            exclude_proposal_id=proposal_id,
            db=db,
        )
        cumulative_spent = await get_cumulative_mandate_spent(
            mandate.mandate_id,
            exclude_proposal_id=proposal_id,
            db=db,
        )
        existing_fp = await get_proposal_by_fingerprint(proposal.fingerprint, db=db)
        existing_ik = None
        if proposal.idempotency_key:
            existing_ik = await get_proposal_by_idempotency_key(
                proposal.idempotency_key, db=db
            )

    context = EvaluationContext(
        now=now,
        today_reserved=today_reserved,
        cumulative_mandate_spent=cumulative_spent,
    )

    decision = evaluate_policy(
        proposal=proposal,
        mandate=mandate,
        context=context,
        existing_by_fingerprint=existing_fp,
        existing_by_idempotency_key=existing_ik,
    )

    if decision.decision == DecisionOutcome.BLOCK:
        # Check if any hard BLOCK rule fired
        for rule in decision.rules:
            if rule.rule_id in HARD_BLOCK_RULES and rule.status == RuleStatus.BLOCK:
                raise ValueError(
                    f"Hard BLOCK rule {rule.rule_id} prevents approval token issuance."
                )
        raise ValueError(
            "Policy evaluation resulted in BLOCK; approval token not issued."
        )

    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=_TOKEN_TTL_SECONDS)
    nonce = secrets.token_hex(16)

    token = generate_approval_token(
        proposal_id=proposal_id,
        content_hash=proposal.content_hash,
        user_session_id=user_session_id,
        approval_timestamp=issued_at,
        nonce=nonce,
        expires_at=expires_at,
    )

    # Atomically register token hash and append audit event in a single transaction
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await register_approval_token(
                token=token,
                proposal_id=proposal_id,
                approval_timestamp=issued_at,
                ttl_seconds=_TOKEN_TTL_SECONDS,
                db=db,
            )
            await AuditService.append_event(
                event_type=AuditEventType.EXCEPTION_APPROVED,
                entity_type=AuditEntityType.PROPOSAL,
                entity_id=proposal_id,
                actor=AuditActor.USER,
                payload={
                    "proposal_id": proposal_id,
                    "user_session_id": user_session_id[:50],
                    "action": "token_issued",
                },
                db=db,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return {
        "proposal_id": proposal_id,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "nonce": nonce,
        "token": token,
    }


async def consume_approval_token(
    proposal_id: str,
    token: str,
    user_session_id: str,
    issued_at_iso: str,
    nonce: str,
) -> tuple[bool, str]:
    """Verify and atomically consume a one-time approval token.

    Args:
        proposal_id: The proposal the token was issued for.
        token: The HMAC-SHA256 hex token.
        user_session_id: The user session bound in the token.
        issued_at_iso: ISO-8601 timestamp when the token was issued (server-issued).
        nonce: Cryptographic nonce bound in the token.

    Returns:
        Tuple of (success, reason).

    Raises:
        RuntimeError: If kill switch is active or unavailable.
        ValueError: If signing key is not configured or parameters invalid.
        ProposalNotFoundError: If the proposal cannot be found.
    """
    # 1. Check kill switch first (fail closed)
    await enforce_kill_switch_guard()

    if not settings.MANDATE_SIGNING_KEY:
        raise ValueError("Signing key not configured.")
    if not nonce or not isinstance(nonce, str) or not nonce.strip():
        return False, "Approval token nonce is missing or empty."

    proposal = await get_proposal(proposal_id)
    mandate = await get_mandate(proposal.mandate_id)

    now = datetime.now(timezone.utc)
    today_iso = now.date().isoformat()

    # 2. Re-evaluate policy independently (backend deterministic verification)
    async with get_connection() as db:
        today_reserved = await get_today_reserved_spend(
            mandate.mandate_id,
            today_iso,
            exclude_proposal_id=proposal_id,
            db=db,
        )
        cumulative_spent = await get_cumulative_mandate_spent(
            mandate.mandate_id,
            exclude_proposal_id=proposal_id,
            db=db,
        )
        existing_fp = await get_proposal_by_fingerprint(proposal.fingerprint, db=db)
        existing_ik = None
        if proposal.idempotency_key:
            existing_ik = await get_proposal_by_idempotency_key(
                proposal.idempotency_key, db=db
            )

    context = EvaluationContext(
        now=now,
        today_reserved=today_reserved,
        cumulative_mandate_spent=cumulative_spent,
    )

    decision = evaluate_policy(
        proposal=proposal,
        mandate=mandate,
        context=context,
        existing_by_fingerprint=existing_fp,
        existing_by_idempotency_key=existing_ik,
    )

    # 3. Check no hard BLOCK rules
    if decision.decision == DecisionOutcome.BLOCK:
        for rule in decision.rules:
            if rule.rule_id in HARD_BLOCK_RULES and rule.status == RuleStatus.BLOCK:
                reason = (
                    f"Hard BLOCK rule {rule.rule_id} cannot be overridden by approval token."
                )
                await _record_consume_failure_audit(proposal_id, user_session_id, reason)
                return False, reason
        reason = "Policy evaluation resulted in BLOCK; approval token cannot override."
        await _record_consume_failure_audit(proposal_id, user_session_id, reason)
        return False, reason

    # 4. Parse issued_at from server-issued value (do NOT trust client timestamp)
    try:
        approval_timestamp = datetime.fromisoformat(issued_at_iso)
        if approval_timestamp.tzinfo is None:
            approval_timestamp = approval_timestamp.replace(tzinfo=timezone.utc)
        else:
            approval_timestamp = approval_timestamp.astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        reason = f"Invalid issued_at timestamp format: {exc}"
        await _record_consume_failure_audit(proposal_id, user_session_id, reason)
        return False, reason

    # 5. Atomically verify and consume the token in SQLite under BEGIN IMMEDIATE
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            success, reason = await verify_and_consume_approval_token(
                token=token,
                proposal_id=proposal_id,
                content_hash=proposal.content_hash,
                user_session_id=user_session_id,
                approval_timestamp=approval_timestamp,
                nonce=nonce,
                now=now,
                ttl_seconds=_TOKEN_TTL_SECONDS,
                db=db,
            )
            if not success:
                await db.rollback()
                await _record_consume_failure_audit(proposal_id, user_session_id, reason)
                return False, reason

            # Token consumed successfully — update proposal status if applicable
            await db.execute(
                """
                UPDATE proposals
                SET status = ?
                WHERE proposal_id = ? AND status IN ('SUBMITTED', 'EVALUATED')
                """,
                (ProposalStatus.APPROVED.value, proposal_id),
            )

            # Atomically append EXCEPTION_APPROVED audit event in the same transaction
            await AuditService.append_event(
                event_type=AuditEventType.EXCEPTION_APPROVED,
                entity_type=AuditEntityType.PROPOSAL,
                entity_id=proposal_id,
                actor=AuditActor.USER,
                payload={
                    "proposal_id": proposal_id,
                    "user_session_id": user_session_id[:50],
                    "action": "token_consumed",
                },
                db=db,
            )
            await db.commit()
            return True, "Approval token consumed successfully."
        except Exception:
            await db.rollback()
            raise


async def _record_consume_failure_audit(
    proposal_id: str,
    user_session_id: str,
    reason: str,
) -> None:
    """Record an audit event for rejected token consumption in its own transaction."""
    try:
        await AuditService.append_event(
            event_type=AuditEventType.REPLAY_ATTEMPT_BLOCKED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=proposal_id,
            actor=AuditActor.USER,
            payload={
                "proposal_id": proposal_id,
                "user_session_id": user_session_id[:50],
                "action": "token_consume_failed",
                "reason": reason[:200],
            },
        )
    except Exception:
        logger.exception("Failed to record failure audit event")
