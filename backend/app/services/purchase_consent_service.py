"""Durable, policy-bound user purchase consent."""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import aiosqlite

from app.core.config import settings
from app.core.security import verify_signature
from app.db.database import (
    get_connection,
    get_mandate_from_db,
    is_kill_switch_active,
)
from app.schemas.audit import AuditActor, AuditEntityType, AuditEventType
from app.schemas.approval import PurchaseConsentResponse
from app.schemas.mandate import MandateStatus
from app.schemas.policy import DecisionOutcome
from app.schemas.proposal import ProposalStatus
from app.services.approval_service import MandateOwnershipError, OwnerlessMandateError
from app.services.audit_service import AuditService
from app.services.intent_auditor_client import IntentAuditorUnavailable
from app.services.intent_audit_service import (
    POLICY_VERSION,
    current_policy_decision,
    mandate_permissions_hash,
    policy_snapshot_hash,
    require_current_assessment,
)
from app.services.mandate_service import MandateNotFoundError
from app.services.proposal_service import ProposalNotFoundError, get_proposal


class PurchaseConsentDeniedError(ValueError):
    """Raised when current policy or lifecycle state disallows purchase consent."""


class PurchaseConsentRequiredError(PurchaseConsentDeniedError):
    """Raised when payment is attempted without a matching durable consent record."""


def _money(value: Decimal) -> str:
    """Return validated proposal money in the repository's canonical representation."""
    if not value.is_finite() or value <= Decimal("0") or value.as_tuple().exponent < -2:
        raise PurchaseConsentDeniedError("Proposal amount is invalid for consent.")
    return f"{value:.2f}"


def _binding_hash(
    *,
    proposal_id: str,
    owner_user_id: str,
    mandate_id: str,
    proposal_content_hash: str,
    mandate_hash: str,
    policy_hash: str,
    decision: DecisionOutcome,
    amount: str,
    currency: str,
    review_token_hash: str | None,
    assessment_binding: str,
) -> str:
    """Hash every authorization fact that the user's consent covers."""
    canonical = json.dumps(
        {
            "amount": amount,
            "assessment_binding": assessment_binding,
            "currency": currency,
            "decision": decision.value,
            "mandate_id": mandate_id,
            "mandate_permissions_hash": mandate_hash,
            "owner_user_id": owner_user_id,
            "policy_snapshot_hash": policy_hash,
            "policy_version": POLICY_VERSION,
            "proposal_content_hash": proposal_content_hash,
            "proposal_id": proposal_id,
            "review_token_hash": review_token_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _current_review_token_hash(
    db: aiosqlite.Connection,
    proposal_id: str,
    owner_user_id: str,
    content_hash: str,
    assessment_binding: str,
) -> str | None:
    cursor = await db.execute(
        """
        SELECT token_hash FROM approval_tokens
        WHERE proposal_id = ? AND owner_user_id = ? AND content_hash = ?
          AND assessment_binding = ? AND consumed_at IS NOT NULL
        ORDER BY consumed_at DESC, rowid DESC LIMIT 1
        """,
        (proposal_id, owner_user_id, content_hash, assessment_binding),
    )
    row = await cursor.fetchone()
    return row[0] if row is not None else None


async def _current_binding(
    proposal_id: str,
    principal_id: str,
    db: aiosqlite.Connection,
    *,
    require_approved: bool,
) -> dict[str, object]:
    """Load and validate all current server-owned facts within one SQLite transaction."""
    try:
        proposal = await get_proposal(proposal_id, db=db)
    except ProposalNotFoundError as exc:
        raise ProposalNotFoundError("Proposal not found.") from exc
    mandate = await get_mandate_from_db(proposal.mandate_id, db=db)
    if mandate is None:
        raise MandateNotFoundError("Mandate not found.")
    if not mandate.owner_user_id:
        raise OwnerlessMandateError("Proposal not found.")
    if mandate.owner_user_id != principal_id:
        raise ProposalNotFoundError("Proposal not found.")
    if not verify_signature(
        mandate.canonical_payload(), mandate.signature, settings.MANDATE_SIGNING_KEY
    ):
        raise PurchaseConsentDeniedError("Mandate integrity verification failed.")

    now = datetime.now(timezone.utc)
    if mandate.status != MandateStatus.ACTIVE or now >= mandate.expires_at:
        raise PurchaseConsentDeniedError("Mandate is not active and within its validity period.")
    if proposal.currency != mandate.currency:
        raise PurchaseConsentDeniedError("Proposal currency does not match the mandate.")
    decision = await current_policy_decision(proposal, mandate, db=db, now=now)
    if decision.decision == DecisionOutcome.BLOCK:
        raise PurchaseConsentDeniedError("Deterministic policy BLOCK prevents purchase.")
    if decision.decision not in (
        DecisionOutcome.ALLOW,
        DecisionOutcome.REQUIRE_APPROVAL,
    ):
        raise PurchaseConsentDeniedError("Current policy outcome is not eligible for consent.")

    if require_approved and proposal.status != ProposalStatus.APPROVED:
        raise PurchaseConsentRequiredError("Purchase consent has not been recorded.")
    if not require_approved and proposal.status not in (
        ProposalStatus.EVALUATED,
        ProposalStatus.APPROVED,
    ):
        raise PurchaseConsentDeniedError("Proposal is not eligible for purchase consent.")

    assessment = await require_current_assessment(proposal, mandate, db=db)
    # The existing payment guard requires an exception token for MEDIUM/HIGH
    # assessments. Since ALLOW cannot use that workflow, fail closed here.
    if (decision.decision == DecisionOutcome.ALLOW
            and assessment["risk_level"] in {"MEDIUM", "HIGH"}):
        raise PurchaseConsentDeniedError(
            "Additional user review is required but this policy outcome does not permit "
            "the exception-review workflow."
        )

    review_hash: str | None = None
    if decision.decision == DecisionOutcome.REQUIRE_APPROVAL:
        review_hash = await _current_review_token_hash(
            db, proposal_id, principal_id, proposal.content_hash,
            assessment["assessment_binding"],
        )
        if review_hash is None:
            raise PurchaseConsentDeniedError(
                "The REQUIRE_APPROVAL exception-review workflow must complete first."
            )

    amount = _money(proposal.total)
    mandate_hash = mandate_permissions_hash(mandate)
    policy_hash = policy_snapshot_hash(decision)
    binding_hash = _binding_hash(
        proposal_id=proposal_id,
        owner_user_id=principal_id,
        mandate_id=mandate.mandate_id,
        proposal_content_hash=proposal.content_hash,
        mandate_hash=mandate_hash,
        policy_hash=policy_hash,
        decision=decision.decision,
        amount=amount,
        currency=proposal.currency,
        review_token_hash=review_hash,
        assessment_binding=assessment["assessment_binding"],
    )
    return {
        "proposal": proposal,
        "mandate": mandate,
        "decision": decision,
        "assessment": assessment,
        "assessment_binding": assessment["assessment_binding"],
        "amount": amount,
        "mandate_hash": mandate_hash,
        "policy_hash": policy_hash,
        "review_hash": review_hash,
        "binding_hash": binding_hash,
        "now": now,
    }


async def record_purchase_consent(
    proposal_id: str,
    principal_id: str,
) -> PurchaseConsentResponse:
    """Record explicit purchase consent atomically with status and audit changes."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            if await is_kill_switch_active(db=db):
                raise RuntimeError("Emergency kill switch is active. Purchase consent halted.")
            facts = await _current_binding(
                proposal_id, principal_id, db, require_approved=False
            )
            cursor = await db.execute(
                "SELECT * FROM purchase_consents WHERE proposal_id = ? AND binding_hash = ?",
                (proposal_id, facts["binding_hash"]),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if (existing["owner_user_id"] != principal_id
                        or existing["proposal_content_hash"]
                        != facts["proposal"].content_hash):
                    raise PurchaseConsentDeniedError(
                        "Existing consent does not match the authenticated owner and proposal."
                    )
                if facts["proposal"].status == ProposalStatus.EVALUATED:
                    raise PurchaseConsentDeniedError(
                        "Consent record and proposal lifecycle state are inconsistent."
                    )
                await db.commit()
                return _response_from_consent(existing, idempotent=True)

            if facts["proposal"].status == ProposalStatus.SETTLED:
                raise PurchaseConsentDeniedError("Settled proposals cannot receive new consent.")

            consent_id = f"pcs_{uuid.uuid4().hex}"
            consented_at = facts["now"].isoformat()
            proposal = facts["proposal"]
            mandate = facts["mandate"]
            decision = facts["decision"]
            await db.execute(
                """
                INSERT INTO purchase_consents (
                    consent_id, proposal_id, owner_user_id, mandate_id,
                    proposal_content_hash, mandate_permissions_hash,
                    policy_snapshot_hash, policy_version, policy_decision,
                    amount, currency, review_token_hash, assessment_binding,
                    binding_hash, consented_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    consent_id,
                    proposal_id,
                    principal_id,
                    mandate.mandate_id,
                    proposal.content_hash,
                    facts["mandate_hash"],
                    facts["policy_hash"],
                    POLICY_VERSION,
                    decision.decision.value,
                    facts["amount"],
                    proposal.currency,
                    facts["review_hash"],
                    facts["assessment_binding"],
                    facts["binding_hash"],
                    consented_at,
                ),
            )
            if proposal.status == ProposalStatus.EVALUATED:
                cursor = await db.execute(
                    "UPDATE proposals SET status = ? WHERE proposal_id = ? AND status = ?",
                    (ProposalStatus.APPROVED.value, proposal_id, ProposalStatus.EVALUATED.value),
                )
                if cursor.rowcount != 1:
                    raise PurchaseConsentDeniedError("Proposal status changed concurrently.")
            await AuditService.append_event(
                event_type=AuditEventType.PURCHASE_CONSENT_RECORDED,
                entity_type=AuditEntityType.PROPOSAL,
                entity_id=proposal_id,
                actor=AuditActor.USER,
                payload={
                    "action": "purchase_consent_recorded",
                    "amount": facts["amount"],
                    "consent_id": consent_id,
                    "currency": proposal.currency,
                    "decision": decision.decision.value,
                    "principal_id": principal_id[:50],
                    "proposal_id": proposal_id,
                },
                db=db,
            )
            await db.commit()
            return PurchaseConsentResponse(
                proposal_id=proposal_id,
                status="APPROVED",
                policy_decision=decision.decision.value,
                amount=Decimal(facts["amount"]),
                currency=proposal.currency,
                consented_at=datetime.fromisoformat(consented_at),
                idempotent=False,
            )
        except Exception:
            await db.rollback()
            raise


def _response_from_consent(
    row: aiosqlite.Row, *, idempotent: bool
) -> PurchaseConsentResponse:
    """Build a minimized response without returning hashes or review identifiers."""
    return PurchaseConsentResponse(
        proposal_id=row["proposal_id"],
        status="APPROVED",
        policy_decision=row["policy_decision"],
        amount=Decimal(row["amount"]),
        currency=row["currency"],
        consented_at=datetime.fromisoformat(row["consented_at"]),
        idempotent=idempotent,
    )


async def require_current_purchase_consent(
    proposal_id: str,
    principal_id: str,
    *,
    db: aiosqlite.Connection | None = None,
) -> None:
    """Fail closed unless an immutable consent matches current authorization facts."""
    async def check(conn: aiosqlite.Connection) -> None:
        if await is_kill_switch_active(db=conn):
            raise RuntimeError("Emergency kill switch is active. Payment execution halted.")
        facts = await _current_binding(
            proposal_id, principal_id, conn, require_approved=True
        )
        cursor = await conn.execute(
            """
            SELECT 1 FROM purchase_consents
            WHERE proposal_id = ? AND owner_user_id = ? AND mandate_id = ?
              AND binding_hash = ? AND proposal_content_hash = ?
              AND mandate_permissions_hash = ? AND policy_snapshot_hash = ?
              AND policy_version = ? AND policy_decision = ?
              AND amount = ? AND currency = ? AND review_token_hash IS ?
              AND assessment_binding = ?
            LIMIT 1
            """,
            (
                proposal_id,
                principal_id,
                facts["mandate"].mandate_id,
                facts["binding_hash"],
                facts["proposal"].content_hash,
                facts["mandate_hash"],
                facts["policy_hash"],
                POLICY_VERSION,
                facts["decision"].decision.value,
                facts["amount"],
                facts["proposal"].currency,
                facts["review_hash"],
                facts["assessment_binding"],
            ),
        )
        if await cursor.fetchone() is None:
            raise PurchaseConsentRequiredError(
                "A current explicit purchase consent is required before payment."
            )

    if db is not None:
        await check(db)
        return
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        await check(conn)
