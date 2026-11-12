"""Content-bound Intent Auditor orchestration and independent gate checks."""

import hashlib
import html
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

import aiosqlite
from pydantic import ValidationError

from app.core.config import settings
from app.db.database import (
    get_connection,
    get_cumulative_mandate_spent,
    get_mandate_from_db,
    get_proposal_by_fingerprint,
    get_proposal_by_idempotency_key,
    get_today_reserved_spend,
    is_kill_switch_active,
)
from app.schemas.audit import AuditActor, AuditEntityType, AuditEventType
from app.schemas.authorization import (
    AuthorizationSnapshotResponse,
    public_policy_decision,
)
from app.schemas.intent_audit import (
    IntentAffectedField,
    IntentAssessmentRecord,
    IntentAssessmentStatus,
    IntentAuditorInput,
    IntentAuditorPolicyRule,
    IntentAuditorResult,
    IntentFindingCode,
    IntentMandateSummary,
    IntentProposalSummary,
    IntentRiskLevel,
    ProposalIntentAssessmentResponse,
    PublicIntentAssessment,
)
from app.schemas.mandate import Mandate
from app.schemas.policy import DecisionOutcome, EvaluationContext, OracleResult, PolicyDecision
from app.schemas.proposal import Proposal
from app.services.audit_service import AuditService
from app.services.intent_auditor_client import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    IntentAuditorClient,
    IntentAuditorUnavailable,
)
from app.services.kill_switch_service import enforce_kill_switch_guard
from app.services.mandate_service import get_mandate
from app.services.policy_engine import evaluate_policy
from app.services.proposal_service import ProposalNotFoundError, get_proposal

logger = logging.getLogger(__name__)
POLICY_VERSION = "P01-P12-v1"
_EXCLUDED_EVIDENCE_KEY_PARTS = (
    "secret", "credential", "token", "session", "owner", "user", "email",
    "signature", "paypal", "fingerprint", "idempotency", "product_source",
    "product_id", "url", "address", "phone",
)
_INJECTION = re.compile(
    r"ignore.{0,50}(instruction|prompt|rules)|system prompt|developer message|"
    r"tool call|reveal.{0,20}(secret|credential)|disregard.{0,30}(instruction|prompt)",
    re.IGNORECASE,
)


class DeterministicBlockDetected(Exception):
    """Raised after a post-model policy recheck persists a new BLOCK."""

    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision


class AssessmentRetrievalError(Exception):
    """Persisted assessment or current-state data could not be read safely."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mandate_permissions_hash(mandate: object) -> str:
    return _sha(mandate.canonical_payload())


def policy_snapshot_hash(decision: PolicyDecision) -> str:
    """Hash deterministic policy outcomes and sanitized, canonical evidence."""
    stable = {
        "decision": decision.decision.value,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "status": rule.status.value,
                "evidence": _canonical_policy_evidence(rule.evidence),
            }
            for rule in sorted(decision.rules, key=lambda item: item.rule_id)
        ],
        "version": POLICY_VERSION,
    }
    serialized = json.dumps(
        stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return _sha(serialized)


def _canonical_policy_evidence(value: object) -> object:
    """Normalize evidence without floats, secrets, identifiers, or unbounded text."""
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key_text = str(key)
            key_lower = key_text.lower()
            if (len(key_text) > 80 or key_lower.endswith("_id")
                    or any(part in key_lower for part in _EXCLUDED_EVIDENCE_KEY_PARTS)):
                continue
            normalized[key_text] = _canonical_policy_evidence(item)
        return normalized
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise ValueError("Policy evidence list exceeds its canonicalization limit.")
        normalized_values = [_canonical_policy_evidence(item) for item in value]
        return sorted(
            normalized_values,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite Decimal policy evidence is invalid.")
        return format(value, "f")
    if isinstance(value, float):
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise ValueError("Non-finite floating policy evidence is invalid.")
        return format(decimal_value, "f")
    if isinstance(value, Enum):
        return _canonical_policy_evidence(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Naive datetime policy evidence is invalid.")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        if len(value) > 512:
            return {"length": len(value), "sha256": _sha(value)}
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    raise TypeError(f"Unsupported policy evidence type: {type(value).__name__}")


def _safe_text(value: str, limit: int) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", value,
                  flags=re.IGNORECASE | re.DOTALL)
    text = html.unescape(re.sub(r"<[^>]*>", " ", text))
    text = " ".join(text.split())[:limit]
    if _INJECTION.search(text):
        return "[untrusted text omitted]"
    return text


async def current_policy_decision(
    proposal: Proposal, mandate: Mandate, *, now: datetime | None = None,
    db: aiosqlite.Connection | None = None, daily_limit: Decimal | None = None,
    oracle_result: OracleResult | None = None,
) -> PolicyDecision:
    """Recompute all policy rules for a proposal using current spend and duplicate state."""
    when = now or datetime.now(timezone.utc)
    async def evaluate(conn: aiosqlite.Connection) -> PolicyDecision:
        today = await get_today_reserved_spend(
            mandate.mandate_id, when.date().isoformat(),
            exclude_proposal_id=proposal.proposal_id, db=conn,
        )
        spent = await get_cumulative_mandate_spent(
            mandate.mandate_id, exclude_proposal_id=proposal.proposal_id, db=conn,
        )
        by_fp = await get_proposal_by_fingerprint(proposal.fingerprint, db=conn)
        by_key = (await get_proposal_by_idempotency_key(proposal.idempotency_key, db=conn)
                  if proposal.idempotency_key else None)
        return evaluate_policy(
            proposal=proposal, mandate=mandate,
            context=EvaluationContext(now=when, today_reserved=today,
                                      cumulative_mandate_spent=spent,
                                      daily_txn_limit=daily_limit,
                                      oracle_result=oracle_result),
            existing_by_fingerprint=by_fp, existing_by_idempotency_key=by_key,
        )
    if db is not None:
        return await evaluate(db)
    async with get_connection() as conn:
        return await evaluate(conn)


async def make_auditor_input(proposal: Proposal, mandate: Mandate,
                             decision: PolicyDecision) -> IntentAuditorInput:
    """Construct the minimized auditor input; internal IDs never leave this function."""
    expires = mandate.expires_at > datetime.now(timezone.utc)
    return IntentAuditorInput(
        mandate=IntentMandateSummary(
            currency=mandate.currency,
            max_total_budget=str(mandate.max_total_budget),
            max_price_per_item=(str(mandate.max_price_per_item)
                                if mandate.max_price_per_item is not None else None),
            max_item_count=mandate.max_item_count,
            max_quantity_per_item=mandate.max_quantity_per_item,
            allowed_merchants=mandate.allowed_merchants,
            allowed_categories=mandate.allowed_categories,
            allow_subscriptions=mandate.allow_subscriptions,
            require_user_approval=mandate.require_user_approval,
            is_active_and_unexpired=(mandate.status.value == "ACTIVE" and expires),
        ),
        proposal=IntentProposalSummary(
            product_name=_safe_text(proposal.product_name, 120),
            merchant=_safe_text(proposal.merchant, 128),
            category=_safe_text(proposal.category, 64),
            quantity=proposal.quantity,
            unit_price=str(proposal.unit_price), total=str(proposal.total),
            currency=proposal.currency,
        ),
        deterministic_outcome=decision.decision.value,
        policy_rules=[IntentAuditorPolicyRule(rule_id=r.rule_id, status=r.status.value)
                      for r in decision.rules],
    )


async def assess_and_persist(
    proposal_id: str, decision: PolicyDecision,
    *, client: IntentAuditorClient | None = None, daily_limit: Decimal | None = None,
    oracle_result: OracleResult | None = None,
) -> IntentAuditorResult:
    """Assess outside SQLite write locks, then bind and atomically persist current result."""
    proposal = await get_proposal(proposal_id)
    mandate = await get_mandate(proposal.mandate_id)
    p_hash = proposal.content_hash
    m_hash = mandate_permissions_hash(mandate)
    policy_hash = policy_snapshot_hash(decision)
    blocked_decision: PolicyDecision | None = None
    try:
        await enforce_kill_switch_guard()
        payload = await make_auditor_input(proposal, mandate, decision)
        result = await (client or IntentAuditorClient()).assess(payload)
        status = IntentAssessmentStatus.VALID
        failure = None
    except Exception as exc:
        result = IntentAuditorResult(schema_version=SCHEMA_VERSION,
                                    risk_level=IntentRiskLevel.UNCERTAIN)
        status = IntentAssessmentStatus.FAILED
        failure = "AUDITOR_UNAVAILABLE" if isinstance(exc, IntentAuditorUnavailable) else "AUDITOR_FAILED"

    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            current_proposal = await get_proposal(proposal_id, db=db)
            current_mandate = await get_mandate(current_proposal.mandate_id, db=db)
            current_decision = await current_policy_decision(
                current_proposal, current_mandate, db=db, daily_limit=daily_limit,
                oracle_result=oracle_result,
            )
            kill_switch_active = await is_kill_switch_active(db=db)
            matches = (
                current_proposal.content_hash == p_hash
                and mandate_permissions_hash(current_mandate) == m_hash
                and policy_snapshot_hash(current_decision) == policy_hash
            )
            if current_decision.decision == DecisionOutcome.BLOCK:
                blocked_decision = current_decision
                status = IntentAssessmentStatus.STALE
                failure = "POLICY_BLOCKED_DURING_AUDIT"
                cursor = await db.execute(
                    "UPDATE proposals SET status = 'REJECTED' WHERE proposal_id = ? "
                    "AND status IN ('SUBMITTED', 'EVALUATED')", (proposal_id,)
                )
                if cursor.rowcount == 1:
                    await AuditService.append_event(
                        event_type=AuditEventType.AUTHORIZATION_BLOCKED,
                        entity_type=AuditEntityType.PROPOSAL, entity_id=proposal_id,
                        actor=AuditActor.POLICY_ENGINE,
                        payload={"proposal_id": proposal_id,
                                 "decision": DecisionOutcome.BLOCK.value,
                                 "blocking_rules": current_decision.blocking_rules}, db=db,
                    )
            elif kill_switch_active:
                status = IntentAssessmentStatus.FAILED
                failure = "KILL_SWITCH_ACTIVE"
                result = IntentAuditorResult(schema_version=SCHEMA_VERSION,
                                            risk_level=IntentRiskLevel.UNCERTAIN)
            elif not matches:
                status = IntentAssessmentStatus.STALE
                failure = "SNAPSHOT_CHANGED"
                result = IntentAuditorResult(schema_version=SCHEMA_VERSION,
                                            risk_level=IntentRiskLevel.UNCERTAIN)
            elif current_mandate.status.value != "ACTIVE" or current_mandate.expires_at <= datetime.now(timezone.utc):
                status = IntentAssessmentStatus.FAILED
                failure = "MANDATE_INACTIVE"
                result = IntentAuditorResult(schema_version=SCHEMA_VERSION,
                                            risk_level=IntentRiskLevel.UNCERTAIN)
            if status == IntentAssessmentStatus.VALID:
                cursor = await db.execute(
                    "SELECT risk_level, finding_codes_json, affected_fields_json FROM "
                    "intent_assessments WHERE proposal_id = ? AND status = 'VALID' "
                    "AND proposal_content_hash = ? AND mandate_permissions_hash = ? "
                    "AND policy_snapshot_hash = ? AND schema_version = ? "
                    "AND prompt_version = ? AND model_identifier = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (proposal_id, p_hash, m_hash, policy_hash, SCHEMA_VERSION,
                     PROMPT_VERSION, settings.active_ai_model),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    result = IntentAuditorResult(
                        schema_version=SCHEMA_VERSION, risk_level=existing[0],
                        finding_codes=json.loads(existing[1]),
                        affected_fields=json.loads(existing[2]),
                    )
                    await db.commit()
                    return result
            assessment_id = f"ia_{uuid.uuid4().hex}"
            created = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO intent_assessments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (assessment_id, proposal_id, current_mandate.mandate_id, status.value,
                 result.risk_level.value,
                 json.dumps([v.value for v in result.finding_codes]),
                 json.dumps([v.value for v in result.affected_fields]), p_hash, m_hash,
                 policy_hash, POLICY_VERSION, SCHEMA_VERSION, PROMPT_VERSION,
                 __import__("app.core.config", fromlist=["settings"]).settings.active_ai_model,
                 failure, created),
            )
            event = (AuditEventType.INTENT_ASSESSMENT_COMPLETED
                     if status == IntentAssessmentStatus.VALID
                     else AuditEventType.INTENT_ASSESSMENT_FAILED)
            await AuditService.append_event(
                event_type=event, entity_type=AuditEntityType.PROPOSAL,
                entity_id=proposal_id, actor=AuditActor.SYSTEM,
                payload={"assessment_id": assessment_id, "status": status.value,
                         "risk_level": result.risk_level.value,
                         "proposal_content_hash": p_hash,
                         "mandate_permissions_hash": m_hash,
                         "policy_snapshot_hash": policy_hash,
                         "finding_codes": [v.value for v in result.finding_codes]}, db=db,
            )
            await db.commit()
        except Exception as exc:
            logger.error(
                "Intent assessment persistence failure: category=%s proposal_id=%s mandate_id=%s status=%s exception=%s",
                "assessment_persistence_failure",
                proposal_id,
                current_mandate.mandate_id if 'current_mandate' in locals() else None,
                status.value if 'status' in locals() else None,
                exc.__class__.__name__,
            )
            await db.rollback()
            raise
    if blocked_decision is not None:
        raise DeterministicBlockDetected(blocked_decision)
    if status != IntentAssessmentStatus.VALID:
        raise IntentAuditorUnavailable("Intent assessment could not be safely completed.")
    return result


async def require_current_assessment(proposal: Proposal, mandate: Mandate,
                                     db: aiosqlite.Connection | None = None,
                                     daily_limit: Decimal | None = None,
                                     oracle_result: OracleResult | None = None,
                                     require_human_review: bool = False) -> dict:
    """Fail closed unless the latest valid assessment binds current deterministic state."""
    async def check(conn: aiosqlite.Connection) -> dict:
        cursor = await conn.execute(
            "SELECT assessment_id, proposal_id, mandate_id, status, risk_level, "
            "finding_codes_json, affected_fields_json, proposal_content_hash, "
            "mandate_permissions_hash, policy_snapshot_hash, policy_version, "
            "schema_version, prompt_version, model_identifier, created_at, failure_code "
            "FROM intent_assessments WHERE proposal_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (proposal.proposal_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            logger.warning(
                "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                "assessment_missing",
                proposal.proposal_id,
                None,
                "MissingAssessmentRow",
            )
            raise IntentAuditorUnavailable("A current valid intent assessment is required.")
        if row[3] != "VALID":
            logger.warning(
                "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                "assessment_stale",
                proposal.proposal_id,
                row[3],
                "AssessmentStatusNotValid",
            )
            raise IntentAuditorUnavailable("A current valid intent assessment is required.")
        if row[4] == "UNCERTAIN":
            logger.warning(
                "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                "assessment_stale",
                proposal.proposal_id,
                row[3],
                "AssessmentRiskUncertain",
            )
            raise IntentAuditorUnavailable(
                "An uncertain intent assessment cannot authorize progression."
            )
        active_versions = (POLICY_VERSION, SCHEMA_VERSION, PROMPT_VERSION, settings.active_ai_model)
        stored_versions = (row[10], row[11], row[12], row[13])
        if (not all(isinstance(version, str) and version for version in stored_versions)
                or stored_versions != active_versions):
            logger.warning(
                "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                "assessment_stale",
                proposal.proposal_id,
                row[3],
                "AssessmentVersionMismatch",
            )
            raise IntentAuditorUnavailable("The intent assessment uses stale auditor versions.")
        current = await current_policy_decision(
            proposal, mandate, db=conn, daily_limit=daily_limit,
            oracle_result=oracle_result,
        )
        if (row[1] != proposal.proposal_id
                or row[2] != mandate.mandate_id
                or row[7] != proposal.content_hash
                or row[8] != mandate_permissions_hash(mandate)
                or row[9] != policy_snapshot_hash(current)
                or current.decision == DecisionOutcome.BLOCK):
            logger.warning(
                "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                "assessment_stale",
                proposal.proposal_id,
                row[3],
                "AssessmentBindingMismatch",
            )
            raise IntentAuditorUnavailable("The intent assessment is stale.")
        try:
            finding_codes = json.loads(row[5])
            affected_fields = json.loads(row[6])
            if not isinstance(finding_codes, list) or not isinstance(affected_fields, list):
                raise ValueError("Assessment fields must be JSON arrays.")
            finding_codes = [IntentFindingCode(value).value for value in finding_codes]
            affected_fields = [IntentAffectedField(value).value for value in affected_fields]
            risk_level = IntentRiskLevel(row[4]).value
            if (len(finding_codes) != len(set(finding_codes))
                    or len(affected_fields) != len(set(affected_fields))):
                raise ValueError("Assessment fields contain duplicate enum values.")
            for digest in (row[7], row[8], row[9]):
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(char not in "0123456789abcdef" for char in digest)):
                    raise ValueError("Assessment snapshot hash is malformed.")
            created_at = datetime.fromisoformat(row[14])
            if created_at.tzinfo is None:
                raise ValueError("Assessment timestamp must be timezone-aware.")
            if not all(isinstance(value, str) and value for value in (row[0], row[1], row[2])):
                raise ValueError("Assessment identifiers are malformed.")
            if row[15] is not None:
                raise ValueError("A VALID assessment cannot contain a failure code.")
            binding_payload = {
                "affected_fields": sorted(affected_fields),
                "assessment_id": row[0],
                "assessment_status": row[3],
                "created_at": row[14],
                "finding_codes": sorted(finding_codes),
                "failure_code": row[15],
                "mandate_id": row[2],
                "mandate_permissions_hash": row[8],
                "model_identifier": row[13],
                "policy_snapshot_hash": row[9],
                "policy_version": row[10],
                "prompt_version": row[12],
                "proposal_content_hash": row[7],
                "proposal_id": row[1],
                "risk_level": risk_level,
                "schema_version": row[11],
            }
            assessment_binding = hashlib.sha256(
                json.dumps(
                    binding_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                "assessment_malformed",
                row[1] if isinstance(row, tuple) and len(row) > 1 else None,
                row[3] if isinstance(row, tuple) and len(row) > 3 else None,
                exc.__class__.__name__,
            )
            raise IntentAuditorUnavailable("Persisted intent assessment is invalid.") from exc
        if (require_human_review and proposal.status.value == "APPROVED"
                and (row[4] in {"MEDIUM", "HIGH"}
                     or current.decision == DecisionOutcome.REQUIRE_APPROVAL)):
            cursor = await conn.execute(
                "SELECT 1 FROM approval_tokens WHERE proposal_id = ? "
                "AND owner_user_id = ? AND content_hash = ? "
                "AND assessment_binding = ? AND consumed_at IS NOT NULL LIMIT 1",
                (proposal.proposal_id, mandate.owner_user_id, proposal.content_hash,
                 assessment_binding),
            )
            if await cursor.fetchone() is None:
                raise IntentAuditorUnavailable("Required user review has not been recorded.")
        return {
            "assessment_binding": assessment_binding,
            "assessment_id": row[0],
            "status": row[3],
            "risk_level": risk_level,
        }
    if db is not None:
        return await check(db)
    async with get_connection() as conn:
        return await check(conn)


def _assessment_record_from_row(row: aiosqlite.Row) -> IntentAssessmentRecord:
    """Validate a persisted row against the internal assessment schema."""
    try:
        return IntentAssessmentRecord.model_validate({
            "assessment_id": row["assessment_id"],
            "proposal_id": row["proposal_id"],
            "mandate_id": row["mandate_id"],
            "status": row["status"],
            "risk_level": row["risk_level"],
            "finding_codes": json.loads(row["finding_codes_json"]),
            "affected_fields": json.loads(row["affected_fields_json"]),
            "proposal_content_hash": row["proposal_content_hash"],
            "mandate_permissions_hash": row["mandate_permissions_hash"],
            "policy_snapshot_hash": row["policy_snapshot_hash"],
            "policy_version": row["policy_version"],
            "schema_version": row["schema_version"],
            "prompt_version": row["prompt_version"],
            "model_identifier": row["model_identifier"],
            "failure_code": row["failure_code"],
            "created_at": row["created_at"],
        })
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise AssessmentRetrievalError("Persisted assessment data is invalid.") from exc


async def _assessment_is_current(
    record: IntentAssessmentRecord,
    proposal: Proposal,
    mandate: Mandate,
    *,
    db: aiosqlite.Connection,
    now: datetime,
) -> bool:
    """Compare all persisted bindings with current, transaction-consistent state."""
    if record.status != IntentAssessmentStatus.VALID:
        return False
    if record.proposal_id != proposal.proposal_id or record.mandate_id != mandate.mandate_id:
        return False
    if record.policy_version != POLICY_VERSION:
        return False
    if record.schema_version != SCHEMA_VERSION:
        return False
    if record.prompt_version != PROMPT_VERSION:
        return False
    if record.model_identifier != settings.active_ai_model:
        return False
    if record.proposal_content_hash != proposal.content_hash:
        return False
    if record.mandate_permissions_hash != mandate_permissions_hash(mandate):
        return False

    decision = await current_policy_decision(proposal, mandate, db=db, now=now)
    return (
        decision.decision != DecisionOutcome.BLOCK
        and record.policy_snapshot_hash == policy_snapshot_hash(decision)
    )


async def get_owner_assessment(
    proposal_id: str, owner_user_id: str
) -> ProposalIntentAssessmentResponse:
    """Read an owner's latest assessment and derive freshness without side effects."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        # The explicit read transaction gives proposal, mandate, and assessment one snapshot.
        await db.execute("BEGIN")
        try:
            proposal = await get_proposal(proposal_id, db=db)
            mandate = await get_mandate_from_db(proposal.mandate_id, db=db)
            if mandate is None or mandate.owner_user_id != owner_user_id:
                raise ProposalNotFoundError("Proposal not found.")

            cursor = await db.execute(
                "SELECT assessment_id, proposal_id, mandate_id, status, risk_level, "
                "finding_codes_json, affected_fields_json, proposal_content_hash, "
                "mandate_permissions_hash, policy_snapshot_hash, policy_version, "
                "schema_version, prompt_version, model_identifier, failure_code, created_at "
                "FROM intent_assessments WHERE proposal_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (proposal_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                logger.warning(
                    "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                    "assessment_missing",
                    proposal_id,
                    None,
                    "MissingAssessmentRow",
                )
                return ProposalIntentAssessmentResponse(
                    proposal_id=proposal_id, assessment=None
                )

            record = _assessment_record_from_row(row)
            if (record.status == IntentAssessmentStatus.FAILED
                    and record.risk_level != IntentRiskLevel.UNCERTAIN):
                logger.warning(
                    "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                    "assessment_malformed",
                    proposal_id,
                    record.status.value,
                    "InvalidFailedAssessment",
                )
                raise AssessmentRetrievalError("Persisted failed assessment data is invalid.")
            try:
                created_at = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
                current = await _assessment_is_current(
                    record, proposal, mandate, db=db, now=datetime.now(timezone.utc)
                )
                public = PublicIntentAssessment(
                    status=record.status,
                    risk_level=record.risk_level,
                    finding_codes=record.finding_codes,
                    affected_fields=record.affected_fields,
                    created_at=created_at,
                    is_current=current,
                )
            except ValidationError as exc:
                logger.warning(
                    "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                    "assessment_malformed",
                    proposal_id,
                    record.status.value,
                    exc.__class__.__name__,
                )
                raise AssessmentRetrievalError(
                    "Persisted assessment data is invalid."
                ) from exc
            except (AttributeError, TypeError, ValueError, aiosqlite.Error) as exc:
                logger.warning(
                    "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                    "assessment_retrieval_failure",
                    proposal_id,
                    record.status.value,
                    exc.__class__.__name__,
                )
                raise AssessmentRetrievalError(
                    "Assessment freshness could not be determined safely."
                ) from exc
            return ProposalIntentAssessmentResponse(
                proposal_id=proposal_id, assessment=public
            )
        except Exception:
            await db.rollback()
            raise


async def get_owner_authorization_snapshot(
    proposal_id: str, owner_user_id: str
) -> AuthorizationSnapshotResponse:
    """Return current policy and auditor state from one read transaction.

    The caller performs policy evaluation and auditor persistence first. This
    read transaction then recomputes policy and derives assessment currentness
    against the same SQLite snapshot, so the two public results cannot describe
    different database revisions.
    """
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        try:
            proposal = await get_proposal(proposal_id, db=db)
            mandate = await get_mandate_from_db(proposal.mandate_id, db=db)
            if mandate is None or mandate.owner_user_id != owner_user_id:
                raise ProposalNotFoundError("Proposal not found.")

            now = datetime.now(timezone.utc)
            decision = await current_policy_decision(
                proposal, mandate, db=db, now=now
            )
            cursor = await db.execute(
                "SELECT assessment_id, proposal_id, mandate_id, status, risk_level, "
                "finding_codes_json, affected_fields_json, proposal_content_hash, "
                "mandate_permissions_hash, policy_snapshot_hash, policy_version, "
                "schema_version, prompt_version, model_identifier, failure_code, created_at "
                "FROM intent_assessments WHERE proposal_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (proposal_id,),
            )
            row = await cursor.fetchone()
            public_assessment = None
            if row is not None:
                try:
                    record = _assessment_record_from_row(row)
                    if (record.status == IntentAssessmentStatus.FAILED
                            and record.risk_level != IntentRiskLevel.UNCERTAIN):
                        logger.warning(
                            "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                            "assessment_malformed",
                            proposal_id,
                            record.status.value,
                            "InvalidFailedAssessment",
                        )
                        raise AssessmentRetrievalError(
                            "Persisted failed assessment data is invalid."
                        )
                    created_at = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
                    current = await _assessment_is_current(
                        record, proposal, mandate, db=db, now=now
                    )
                    public_assessment = PublicIntentAssessment(
                        status=record.status,
                        risk_level=record.risk_level,
                        finding_codes=record.finding_codes,
                        affected_fields=record.affected_fields,
                        created_at=created_at,
                        is_current=current,
                    )
                except (AssessmentRetrievalError, ValidationError) as exc:
                    logger.warning(
                        "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                        "assessment_retrieval_failure",
                        proposal_id,
                        getattr(row, "status", None),
                        exc.__class__.__name__,
                    )
                    raise
                except (AttributeError, TypeError, ValueError, aiosqlite.Error) as exc:
                    logger.warning(
                        "Intent assessment retrieval failure: category=%s proposal_id=%s status=%s exception=%s",
                        "assessment_retrieval_failure",
                        proposal_id,
                        getattr(row, "status", None),
                        exc.__class__.__name__,
                    )
                    raise AssessmentRetrievalError(
                        "Authorization snapshot data is invalid or unavailable."
                    ) from exc

            result = AuthorizationSnapshotResponse(
                proposal_id=proposal_id,
                proposal_status=proposal.status,
                policy_decision=public_policy_decision(decision),
                assessment=public_assessment,
            )
            await db.commit()
            return result
        except (AssessmentRetrievalError, ProposalNotFoundError):
            await db.rollback()
            raise
        except Exception as exc:
            await db.rollback()
            if isinstance(exc, (ValidationError, aiosqlite.Error, TypeError, ValueError)):
                raise AssessmentRetrievalError(
                    "Authorization snapshot data is invalid or unavailable."
                ) from exc
            raise
