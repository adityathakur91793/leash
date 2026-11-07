"""One-time exception approval token service.

Handles issuing and consuming short-lived HMAC-SHA256 approval tokens for
proposals that require user approval (soft-approval cases only).

Hard BLOCK rules (P01, P02, P03, P08, P11, P12) cannot be overridden by
any approval token.
All token registration, consumption, state transitions, and audit records
are committed atomically in single SQLite transactions under BEGIN IMMEDIATE.

Ownership enforcement:
- Mandates without an owner (owner_user_id IS NULL) fail closed with an error.
  Legacy mandates created before Migration 6 are ownerless and cannot be
  approved via the token system. Do NOT silently assign owners to them.
- Only the principal that created the mandate (mandate.owner_user_id) may
  issue or consume approval tokens for its proposals.
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
    is_kill_switch_active,
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


class OwnerlessMandateError(Exception):
    """Raised when an approval operation is attempted on an ownerless legacy mandate."""


class MandateOwnershipError(Exception):
    """Raised when a principal attempts an approval operation on a mandate they do not own."""


# Hard BLOCK rule IDs that cannot be overridden by any approval token
HARD_BLOCK_RULES: frozenset[str] = frozenset({"P01", "P02", "P03", "P08", "P11", "P12"})

_TOKEN_TTL_SECONDS = 900  # 15 minutes


async def issue_approval_token(
    proposal_id: str,
    principal_id: str,
) -> dict:
    """Issue a short-lived HMAC-SHA256 approval token for a soft-approval proposal.

    Args:
        proposal_id: The proposal to bind the token to.
        principal_id: The authenticated principal identifier from the session.
            Must match mandate.owner_user_id.

    Returns:
        dict with keys: proposal_id, issued_at, expires_at, nonce, token.

    Raises:
        ValueError: If signing key is not configured, proposal is hard-blocked,
                    mandate is ownerless, or principal does not own the mandate.
        MandateNotFoundError: If the associated mandate cannot be found.
    """
    if not settings.MANDATE_SIGNING_KEY:
        raise ValueError("Signing key not configured.")
    await enforce_kill_switch_guard()

    proposal = await get_proposal(proposal_id)
    mandate = await get_mandate(proposal.mandate_id)


    # Ownership check: fail closed for ownerless mandates and wrong principal
    if mandate.owner_user_id is None:
        raise OwnerlessMandateError(
            "Ownerless mandate: approval token issuance is not permitted for mandates "
            "without an owner. Mandates created before session authentication was "
            "introduced cannot be approved via the token system."
        )
    if mandate.owner_user_id != principal_id:
        raise MandateOwnershipError(
            "Forbidden: the authenticated principal does not own this mandate."
        )

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

    from app.services.intent_audit_service import require_current_assessment
    await require_current_assessment(proposal, mandate)
    if proposal.status != ProposalStatus.EVALUATED:
        raise ValueError("Only evaluated proposals requiring user review may receive a token.")

    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=_TOKEN_TTL_SECONDS)
    nonce = secrets.token_hex(16)

    token = generate_approval_token(
        proposal_id=proposal_id,
        content_hash=proposal.content_hash,
        user_session_id=principal_id,
        approval_timestamp=issued_at,
        nonce=nonce,
        expires_at=expires_at,
    )

    # Atomically register token hash and append audit event in a single transaction
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if await is_kill_switch_active(db=db):
                raise RuntimeError("Emergency kill switch is active. Approval halted.")
            current_proposal = await get_proposal(proposal_id, db=db)
            current_mandate = await get_mandate(current_proposal.mandate_id, db=db)
            assessment = await require_current_assessment(current_proposal, current_mandate, db=db)
            if current_proposal.status != ProposalStatus.EVALUATED:
                raise ValueError("Proposal is no longer awaiting user review.")
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
                    "principal_id": principal_id[:50],
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
    principal_id: str,
    issued_at_iso: str,
    nonce: str,
) -> tuple[bool, str]:
    """Verify and atomically consume a one-time approval token.

    Args:
        proposal_id: The proposal the token was issued for.
        token: The HMAC-SHA256 hex token.
        principal_id: The authenticated principal identifier from the session.
            Must match mandate.owner_user_id.
        issued_at_iso: ISO-8601 timestamp when the token was issued (server-issued).
        nonce: Cryptographic nonce bound in the token.

    Returns:
        Tuple of (success, reason).

    Raises:
        RuntimeError: If kill switch is active or unavailable.
        ValueError: If signing key is not configured, mandate is ownerless,
                    principal does not own the mandate, or parameters invalid.
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

    # Ownership check: fail closed for ownerless mandates and wrong principal
    if mandate.owner_user_id is None:
        raise OwnerlessMandateError(
            "Ownerless mandate: approval token consumption is not permitted. "
            "This mandate was created before session authentication was introduced."
        )
    if mandate.owner_user_id != principal_id:
        raise MandateOwnershipError(
            "Forbidden: the authenticated principal does not own this mandate."
        )

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

    from app.services.intent_audit_service import require_current_assessment
    try:
        await require_current_assessment(proposal, mandate)
    except Exception as exc:
        await _record_consume_failure_audit(proposal_id, principal_id, "Intent assessment missing or stale.")
        return False, "A current valid intent assessment is required."

    # 3. Check no hard BLOCK rules
    if decision.decision == DecisionOutcome.BLOCK:
        for rule in decision.rules:
            if rule.rule_id in HARD_BLOCK_RULES and rule.status == RuleStatus.BLOCK:
                reason = (
                    f"Hard BLOCK rule {rule.rule_id} cannot be overridden by approval token."
                )
                await _record_consume_failure_audit(proposal_id, principal_id, reason)
                return False, reason
        reason = "Policy evaluation resulted in BLOCK; approval token cannot override."
        await _record_consume_failure_audit(proposal_id, principal_id, reason)
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
        await _record_consume_failure_audit(proposal_id, principal_id, reason)
        return False, reason

    # 5. Atomically verify and consume the token in SQLite under BEGIN IMMEDIATE
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if await is_kill_switch_active(db=db):
                raise RuntimeError("Emergency kill switch is active. Approval halted.")
            current_proposal = await get_proposal(proposal_id, db=db)
            current_mandate = await get_mandate(current_proposal.mandate_id, db=db)
            await require_current_assessment(current_proposal, current_mandate, db=db)
            success, reason = await verify_and_consume_approval_token(
                token=token,
                proposal_id=proposal_id,
                content_hash=proposal.content_hash,
                user_session_id=principal_id,
                approval_timestamp=approval_timestamp,
                nonce=nonce,
                now=now,
                ttl_seconds=_TOKEN_TTL_SECONDS,
                db=db,
            )
            if not success:
                await db.rollback()
                await _record_consume_failure_audit(proposal_id, principal_id, reason)
                return False, reason

            if current_proposal.status != ProposalStatus.EVALUATED:
                await db.rollback()
                return False, "Proposal is no longer awaiting user review."

            # Token consumed successfully — update proposal status if applicable
            cursor = await db.execute(
                """
                UPDATE proposals
                SET status = ?
                WHERE proposal_id = ? AND status IN ('SUBMITTED', 'EVALUATED')
                """,
                (ProposalStatus.APPROVED.value, proposal_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Proposal approval state changed concurrently.")

            # Atomically append EXCEPTION_APPROVED audit event in the same transaction
            await AuditService.append_event(
                event_type=AuditEventType.EXCEPTION_APPROVED,
                entity_type=AuditEntityType.PROPOSAL,
                entity_id=proposal_id,
                actor=AuditActor.USER,
                payload={
                    "proposal_id": proposal_id,
                    "principal_id": principal_id[:50],
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
    principal_id: str,
    reason: str,
) -> None:
    """Record an audit event for rejected token consumption in its own transaction.

    Args:
        proposal_id: The proposal whose token consumption failed.
        principal_id: The principal that attempted consumption.
        reason: Human-readable failure reason.
    """
    try:
        await AuditService.append_event(
            event_type=AuditEventType.REPLAY_ATTEMPT_BLOCKED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=proposal_id,
            actor=AuditActor.USER,
            payload={
                "proposal_id": proposal_id,
                "principal_id": principal_id[:50],
                "action": "token_consume_failed",
                "reason": reason[:200],
            },
        )
    except Exception:
        logger.exception("Failed to record failure audit event")
