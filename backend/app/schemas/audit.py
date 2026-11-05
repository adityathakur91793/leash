"""Pydantic schemas and canonical hashing helpers for the audit logging subsystem."""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import hmac
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuditEventType(str, Enum):
    """Categorized lifecycle events recorded in the audit log."""

    GENESIS = "GENESIS"
    MANDATE_CREATED = "MANDATE_CREATED"
    MANDATE_CONFIRMED = "MANDATE_CONFIRMED"
    MANDATE_SIGNED = "MANDATE_SIGNED"
    MANDATE_ACTIVATED = "MANDATE_ACTIVATED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    PROPOSAL_CREATED = "PROPOSAL_CREATED"
    PROPOSAL_EVALUATED = "PROPOSAL_EVALUATED"
    PROPOSAL_APPROVED = "PROPOSAL_APPROVED"
    PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
    EXCEPTION_APPROVED = "EXCEPTION_APPROVED"
    REPLAY_ATTEMPT_BLOCKED = "REPLAY_ATTEMPT_BLOCKED"
    AUTHORIZATION_BLOCKED = "AUTHORIZATION_BLOCKED"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    KILL_SWITCH_DISENGAGED = "KILL_SWITCH_DISENGAGED"
    CHECKPOINT_SEALED = "CHECKPOINT_SEALED"


class AuditEntityType(str, Enum):
    """Target entity classification."""

    MANDATE = "MANDATE"
    PROPOSAL = "PROPOSAL"
    SYSTEM = "SYSTEM"


class AuditActor(str, Enum):
    """Initiating actor classification."""

    USER = "USER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"
    ADMIN = "ADMIN"
    POLICY_ENGINE = "POLICY_ENGINE"


def _assert_no_floats(obj: Any, path: str = "root") -> None:
    """Recursively reject float and Decimal values in canonical audit payloads.

    Both types are forbidden because:
    - ``float`` values are inherently imprecise and produce non-deterministic
      representations across platforms.
    - ``Decimal`` values are not natively JSON-serializable and would cause an
      unhandled ``TypeError`` inside ``json.dumps``.

    Monetary values MUST be pre-serialized to fixed two-decimal strings
    (e.g. ``"19.99"``) before being placed in an audit envelope.
    """
    if isinstance(obj, float):
        raise ValueError(
            "Floating-point numbers are strictly forbidden in canonical audit envelopes "
            f"at '{path}'."
        )
    if isinstance(obj, Decimal):
        raise ValueError(
            "Decimal values are not permitted in canonical audit envelopes "
            f"at '{path}'. Convert monetary amounts to fixed two-decimal strings "
            "before hashing (e.g. str(value.quantize(Decimal('0.01'))))."
        )
    if isinstance(obj, dict):
        for k, v in obj.items():
            _assert_no_floats(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple, set)):
        for idx, item in enumerate(obj):
            _assert_no_floats(item, f"{path}[{idx}]")


def canonical_audit_hash(envelope: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hash of an audit envelope.

    This function implements a **Restricted Deterministic JSON Profile** tailored
    for LEASH audit log entries. It is NOT a general-purpose RFC 8785 / JCS
    implementation; it differs from RFC 8785 in the following ways:

    - ``float`` and ``Decimal`` values are explicitly rejected (RFC 8785 defines
      canonical float formatting; LEASH forbids both to eliminate precision risk).
    - Dictionary key ordering uses Python's Unicode code-point sort, which
      diverges from RFC 8785's UTF-16 code-unit sort for keys containing
      supplementary-plane characters (code points > U+FFFF). All LEASH envelope
      keys are ASCII, so this divergence has no practical effect.

    Guarantees:
    1. Rejection of ``float`` and ``Decimal`` payload values.
    2. Lexicographically sorted dictionary keys at all levels (Python code-point order).
    3. Compact separators — no superfluous whitespace.
    4. UTF-8 serialization with non-ASCII characters left unescaped.
    5. Monetary values MUST be passed as fixed two-decimal strings (e.g. ``"19.99"``).
    """
    _assert_no_floats(envelope)
    raw_bytes = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest()


def sign_checkpoint(
    sequence_number: int,
    entry_hash: str,
    sealed_at: datetime,
    secret_key: str,
) -> str:
    """Compute HMAC-SHA256 signature for a checkpoint anchor."""
    ts_iso = sealed_at.astimezone(timezone.utc).isoformat()
    payload = {
        "entry_hash": entry_hash,
        "sealed_at": ts_iso,
        "sequence_number": sequence_number,
    }
    raw_bytes = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()


class AuditLogEntry(BaseModel):
    """Immutable audit record representing an event in the hash-chain."""

    model_config = ConfigDict(extra="forbid")

    sequence_number: int = Field(
        ..., ge=1, description="Monotonic sequential index starting at 1."
    )
    timestamp: datetime = Field(..., description="Timezone-aware UTC timestamp of event.")
    event_type: str = Field(..., description="Domain event category.")
    entity_type: str = Field(..., description="Target entity domain.")
    entity_id: str = Field(..., description="Target entity identifier.")
    actor: str = Field(..., description="Initiating actor.")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Structured event payload."
    )
    prev_hash: str = Field(
        ..., min_length=64, max_length=64, description="SHA-256 of previous record."
    )
    entry_hash: str = Field(
        ..., min_length=64, max_length=64, description="Canonical SHA-256 of this record."
    )

    @field_validator("timestamp", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    def to_envelope(self) -> dict[str, Any]:
        """Convert entry into dictionary structure for canonical hash computation."""
        return {
            "actor": self.actor,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "event_type": self.event_type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "sequence_number": self.sequence_number,
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat(),
        }

    def compute_canonical_hash(self) -> str:
        """Compute the expected canonical hash for this entry."""
        return canonical_audit_hash(self.to_envelope())


class AuditCheckpoint(BaseModel):
    """Cryptographically signed anchor sealing the audit chain up to a sequence number."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_id: int | None = Field(
        default=None, description="Primary key assigned by database."
    )
    sequence_number: int = Field(
        ..., ge=1, description="Audit chain tip sequence at time of sealing."
    )
    entry_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="Hash of the record at sequence_number.",
    )
    sealed_at: datetime = Field(..., description="Timezone-aware UTC timestamp of seal.")
    sealed_by: str = Field(..., description="Initiating authority or process.")
    checkpoint_sig: str = Field(
        ..., min_length=64, max_length=64, description="HMAC-SHA256 signature."
    )

    @field_validator("sealed_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class AuditLogResponse(BaseModel):
    """Paginated list of audit log records."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0, description="Total matching audit records.")
    limit: int = Field(..., ge=1, le=100, description="Page size.")
    offset: int = Field(..., ge=0, description="Page offset.")
    items: list[AuditLogEntry] = Field(
        default_factory=list, description="Audit entries in sequence order."
    )


DEFAULT_CHECKPOINT_CADENCE: int = 50


class AuditVerifyResponse(BaseModel):
    """Cryptographic audit chain verification result."""

    model_config = ConfigDict(extra="forbid")

    valid: bool = Field(
        ..., description="True if hash-chain and checkpoints are verified without tampering."
    )
    total_records: int = Field(..., ge=0, description="Number of audit log records checked.")
    total_checkpoints: int = Field(
        default=0, ge=0, description="Number of audit checkpoints checked."
    )
    verified_sequence_range: tuple[int, int] | None = Field(
        default=None, description="Span of sequences successfully verified [start, end]."
    )
    latest_entry_hash: str | None = Field(default=None, description="Hash at the verified tip.")
    latest_checkpoint_sequence: int | None = Field(
        default=None, description="Sequence number of the latest verified checkpoint."
    )
    corrupted_sequence: int | None = Field(
        default=None, description="Sequence number where tampering or gap was detected, if any."
    )
    error_message: str | None = Field(
        default=None, description="Specific integrity error or truncation warning, if any."
    )
    anomalies: list[str] = Field(
        default_factory=list, description="List of detected anomaly classification tags."
    )
