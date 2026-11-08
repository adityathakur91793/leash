"""Lightweight async SQLite database helpers using aiosqlite."""

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.config import BACKEND_DIR, settings
from app.core.security import proposal_content_hash
from app.schemas.audit import (
    AuditCheckpoint,
    AuditLogEntry,
    canonical_audit_hash,
    sign_checkpoint,
)
from app.schemas.kill_switch import KillSwitchStatus
from app.schemas.mandate import Mandate, MandateStatus
from app.schemas.payment import PayPalOrderRecord, PayPalOrderStatus
from app.schemas.proposal import Proposal, ProposalStatus

logger = logging.getLogger(__name__)


class StoredProposalTamperedError(Exception):
    """Raised when a stored proposal fails content-hash integrity verification."""


def get_db_path(url: str | None = None) -> str:
    """Return the filesystem path derived from ``DATABASE_URL``.

    Relative paths are resolved deterministically against the backend directory.
    Absolute paths and ':memory:' remain unchanged.
    """
    db_url = url if url is not None else settings.DATABASE_URL
    if db_url.startswith("sqlite:///"):
        raw_path = db_url.removeprefix("sqlite:///")
    else:
        raw_path = db_url

    if raw_path == ":memory:":
        return ":memory:"

    path_obj = Path(raw_path)
    if not path_obj.is_absolute():
        path_obj = (BACKEND_DIR / path_obj).resolve()
    return str(path_obj)


@asynccontextmanager
async def get_connection(url: str | None = None) -> AsyncIterator[aiosqlite.Connection]:
    """Open a SQLite connection with foreign-key enforcement enabled."""
    db_path = get_db_path(url)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


async def init_db(*, _target_version: int | None = None) -> None:
    """Create database tables and apply incremental schema migrations."""
    target_version = 10 if _target_version is None else _target_version
    if not 1 <= target_version <= 10:
        raise ValueError("Database migration target must be between versions 1 and 10.")
    db_path: str = get_db_path()

    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    logger.info("Initialising database at %s", db_path)

    async with get_connection() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER NOT NULL,
                applied_at TEXT    NOT NULL
            )
            """
        )

        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        current_version = row[0] if row and row[0] is not None else 0

        # Migration 1: Initial bootstrap
        if current_version < 1 and target_version >= 1:
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (1, datetime('now'))"
            )
            current_version = 1
            logger.info("Applied migration version 1")

        # Migration 2: Mandates table (Phase 2A)
        if current_version < 2 and target_version >= 2:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mandates (
                    mandate_id            TEXT PRIMARY KEY,
                    schema_version        TEXT NOT NULL,
                    status                TEXT NOT NULL,
                    currency              TEXT NOT NULL,
                    max_total_budget      TEXT NOT NULL,
                    max_price_per_item    TEXT,
                    max_item_count        INTEGER NOT NULL,
                    max_quantity_per_item INTEGER NOT NULL,
                    allow_subscriptions   INTEGER NOT NULL,
                    require_user_approval INTEGER NOT NULL,
                    allowed_merchants     TEXT NOT NULL,
                    allowed_categories    TEXT NOT NULL,
                    created_at            TEXT NOT NULL,
                    expires_at            TEXT NOT NULL,
                    signature             TEXT,
                    signed_at             TEXT,
                    signature_algorithm   TEXT
                )
                """
            )
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (2, datetime('now'))"
            )
            current_version = 2
            logger.info("Applied migration version 2 (mandates)")

        # Migration 3: Purchase proposals (Phase 2B)
        if current_version < 3 and target_version >= 3:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id      TEXT PRIMARY KEY,
                    schema_version   TEXT NOT NULL,
                    status           TEXT NOT NULL,
                    mandate_id       TEXT NOT NULL,
                    product_id       TEXT NOT NULL,
                    product_name     TEXT NOT NULL,
                    merchant         TEXT NOT NULL,
                    category         TEXT NOT NULL,
                    quantity         INTEGER NOT NULL,
                    unit_price       TEXT NOT NULL,
                    currency         TEXT NOT NULL,
                    subtotal         TEXT NOT NULL,
                    tax              TEXT NOT NULL,
                    shipping         TEXT NOT NULL,
                    other_charges    TEXT NOT NULL,
                    total            TEXT NOT NULL,
                    product_source   TEXT NOT NULL,
                    created_at       TEXT NOT NULL,
                    idempotency_key  TEXT,
                    fingerprint      TEXT NOT NULL,
                    content_hash     TEXT NOT NULL,
                    FOREIGN KEY (mandate_id) REFERENCES mandates(mandate_id)
                )
                """
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_fingerprint "
                "ON proposals(fingerprint)"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_idempotency_key "
                "ON proposals(idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_proposals_mandate_id "
                "ON proposals(mandate_id)"
            )
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (3, datetime('now'))"
            )
            current_version = 3
            logger.info("Applied migration version 3 (proposals)")

        # Migration 4: Approval tokens tracking (Phase 2C Remediation)
        if current_version < 4 and target_version >= 4:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_tokens (
                    token_hash    TEXT PRIMARY KEY,
                    proposal_id   TEXT NOT NULL,
                    expires_at    TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    consumed_at   TEXT,
                    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_approval_tokens_proposal_id "
                "ON approval_tokens(proposal_id)"
            )
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (4, datetime('now'))"
            )
            current_version = 4
            logger.info("Applied migration version 4 (approval_tokens)")

        # Migration 5: Audit logs, checkpoints, and system controls (Phase 2D.1)
        if current_version < 5 and target_version >= 5:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    sequence_number  INTEGER PRIMARY KEY,
                    timestamp        TEXT    NOT NULL,
                    event_type       TEXT    NOT NULL,
                    entity_type      TEXT    NOT NULL,
                    entity_id        TEXT    NOT NULL,
                    actor            TEXT    NOT NULL,
                    payload_json     TEXT    NOT NULL,
                    prev_hash        TEXT    NOT NULL,
                    entry_hash       TEXT    NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_logs_entity "
                "ON audit_logs(entity_type, entity_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_logs_event_type "
                "ON audit_logs(event_type)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp "
                "ON audit_logs(timestamp)"
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_checkpoints (
                    checkpoint_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    sequence_number  INTEGER NOT NULL,
                    entry_hash       TEXT    NOT NULL,
                    sealed_at        TEXT    NOT NULL,
                    sealed_by        TEXT    NOT NULL,
                    checkpoint_sig   TEXT    NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_checkpoints_seq "
                "ON audit_checkpoints(sequence_number)"
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS system_controls (
                    control_id         INTEGER PRIMARY KEY CHECK (control_id = 1),
                    kill_switch_active INTEGER NOT NULL DEFAULT 0,
                    engaged_at         TEXT,
                    engaged_by         TEXT,
                    reason             TEXT,
                    updated_at         TEXT    NOT NULL
                )
                """
            )
            now_utc_str = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """
                INSERT OR IGNORE INTO system_controls (
                    control_id, kill_switch_active, engaged_at, engaged_by, reason, updated_at
                ) VALUES (1, 0, NULL, NULL, NULL, ?)
                """,
                (now_utc_str,),
            )

            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (5, datetime('now'))"
            )
            current_version = 5
            logger.info(
                "Applied migration version 5 (audit_logs, audit_checkpoints, system_controls)"
            )

        # Ensure Genesis Audit Log & Checkpoint exist whenever audit_logs is empty
        if current_version >= 5:
            # Enforce UNIQUE constraint on audit_checkpoints.sequence_number.
            # Migration 5 may have been applied with only a non-unique index on
            # already-running databases. We upgrade it here idempotently.
            # Step 1: Check for duplicate sequence_number rows (should never exist,
            # but we refuse to silently delete records if they somehow do).
            dup_cursor = await db.execute(
                """
                SELECT sequence_number, COUNT(*) AS cnt
                FROM audit_checkpoints
                GROUP BY sequence_number
                HAVING cnt > 1
                """
            )
            duplicates = await dup_cursor.fetchall()
            if duplicates:
                dup_info = ", ".join(
                    f"seq={r[0]} (count={r[1]})" for r in duplicates
                )
                raise RuntimeError(
                    "CRITICAL: Duplicate sequence_number rows detected in "
                    f"audit_checkpoints: {dup_info}. "
                    "Data integrity violation — refusing to apply UNIQUE constraint. "
                    "Investigate and resolve duplicates manually before restarting."
                )
            # Step 2: Drop the old non-unique index if it still exists, then
            # create the enforced UNIQUE index. Both ops are idempotent.
            existing = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_audit_checkpoints_seq'"
            )
            idx_row = await existing.fetchone()
            if idx_row is not None:
                idx_info = await db.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='index' AND name='idx_audit_checkpoints_seq'"
                )
                idx_sql_row = await idx_info.fetchone()
                idx_sql = idx_sql_row[0] if idx_sql_row else ""
                if idx_sql and "UNIQUE" not in idx_sql.upper():
                    await db.execute("DROP INDEX idx_audit_checkpoints_seq")
                    logger.info(
                        "Dropped non-unique idx_audit_checkpoints_seq; "
                        "recreating as UNIQUE"
                    )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_checkpoints_seq "
                "ON audit_checkpoints(sequence_number)"
            )

            cursor = await db.execute("SELECT COUNT(*) FROM audit_logs")
            count = (await cursor.fetchone())[0]
            if count == 0:
                genesis_now = datetime.now(timezone.utc)
                genesis_ts = genesis_now.isoformat()
                genesis_prev_hash = "0" * 64
                genesis_payload = {"message": "LEASH audit logging subsystem initialized."}
                genesis_envelope = {
                    "actor": "SYSTEM",
                    "entity_id": "system",
                    "entity_type": "SYSTEM",
                    "event_type": "GENESIS",
                    "payload": genesis_payload,
                    "prev_hash": genesis_prev_hash,
                    "sequence_number": 1,
                    "timestamp": genesis_ts,
                }
                genesis_entry_hash = canonical_audit_hash(genesis_envelope)
                await db.execute(
                    """
                    INSERT INTO audit_logs (
                        sequence_number, timestamp, event_type, entity_type,
                        entity_id, actor, payload_json, prev_hash, entry_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        genesis_ts,
                        "GENESIS",
                        "SYSTEM",
                        "system",
                        "SYSTEM",
                        json.dumps(genesis_payload, sort_keys=True, separators=(",", ":")),
                        genesis_prev_hash,
                        genesis_entry_hash,
                    ),
                )

                # Seed Genesis Checkpoint.
                # MANDATE_SIGNING_KEY is validated to be non-empty and >= 32 bytes at
                # Settings() construction time. No fallback is permitted here.
                if not settings.MANDATE_SIGNING_KEY:
                    raise RuntimeError(
                        "MANDATE_SIGNING_KEY is required to initialize the genesis "
                        "audit checkpoint. Ensure the key is set before starting the server."
                    )
                genesis_sig = sign_checkpoint(
                    1, genesis_entry_hash, genesis_now, settings.MANDATE_SIGNING_KEY
                )
                await db.execute(
                    """
                    INSERT INTO audit_checkpoints (
                        sequence_number, entry_hash, sealed_at, sealed_by, checkpoint_sig
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (1, genesis_entry_hash, genesis_ts, "SYSTEM_INIT", genesis_sig),
                )

        # Migration 6: User sessions and mandate ownership
        if current_version < 6 and target_version >= 6:
            # user_sessions table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    session_token_hash  TEXT PRIMARY KEY,
                    user_id             TEXT NOT NULL,
                    created_at          TEXT NOT NULL,
                    expires_at          TEXT NOT NULL,
                    revoked_at          TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id "
                "ON user_sessions(user_id)"
            )
            # Add owner_user_id column to mandates (idempotent)
            try:
                await db.execute("ALTER TABLE mandates ADD COLUMN owner_user_id TEXT")
            except aiosqlite.OperationalError:
                pass  # Column already exists (idempotent re-run)
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (6, datetime('now'))"
            )
            current_version = 6
            logger.info(
                "Applied migration version 6 (user_sessions, mandate owner_user_id)"
            )

        # Migration 7: PayPal Orders and Payment Execution (Phase 3)
        if current_version < 7 and target_version >= 7:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS paypal_orders (
                    order_id            TEXT PRIMARY KEY,
                    proposal_id         TEXT NOT NULL UNIQUE,
                    mandate_id          TEXT NOT NULL,
                    owner_user_id       TEXT NOT NULL,
                    status              TEXT NOT NULL,
                    currency            TEXT NOT NULL,
                    total_amount        TEXT NOT NULL,
                    subtotal            TEXT NOT NULL,
                    tax                 TEXT NOT NULL,
                    shipping            TEXT NOT NULL,
                    other_charges       TEXT NOT NULL,
                    approval_url        TEXT NOT NULL,
                    paypal_request_id   TEXT NOT NULL,
                    capture_id          TEXT,
                    capture_request_id  TEXT,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL,
                    captured_at         TEXT,
                    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id),
                    FOREIGN KEY (mandate_id) REFERENCES mandates(mandate_id)
                )
            """)
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_paypal_orders_proposal "
                "ON paypal_orders(proposal_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_paypal_orders_mandate "
                "ON paypal_orders(mandate_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_paypal_orders_owner "
                "ON paypal_orders(owner_user_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_paypal_orders_status "
                "ON paypal_orders(status)"
            )
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (7, datetime('now'))"
            )
            current_version = 7
            logger.info("Applied migration version 7 (paypal_orders)")

        # Migration 8: content-bound Intent Auditor assessments (Phase 5).
        if current_version < 8 and target_version >= 8:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS intent_assessments (
                    assessment_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    mandate_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    finding_codes_json TEXT NOT NULL,
                    affected_fields_json TEXT NOT NULL,
                    proposal_content_hash TEXT NOT NULL,
                    mandate_permissions_hash TEXT NOT NULL,
                    policy_snapshot_hash TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_identifier TEXT NOT NULL,
                    failure_code TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id),
                    FOREIGN KEY (mandate_id) REFERENCES mandates(mandate_id)
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_intent_assessments_proposal "
                "ON intent_assessments(proposal_id, created_at)"
            )
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (8, datetime('now'))"
            )
            current_version = 8
            logger.info("Applied migration version 8 (intent_assessments)")

        # Migration 9: durable, content-bound purchase consent (Phase 7F.2A).
        if current_version < 9 and target_version >= 9:
            # Historical token rows cannot serve as review evidence for the new
            # consent contract because their owner/content bindings were not stored.
            await db.execute("ALTER TABLE approval_tokens ADD COLUMN owner_user_id TEXT")
            await db.execute("ALTER TABLE approval_tokens ADD COLUMN content_hash TEXT")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_approval_tokens_review_binding "
                "ON approval_tokens(proposal_id, owner_user_id, content_hash, consumed_at)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS purchase_consents (
                    consent_id                 TEXT PRIMARY KEY,
                    proposal_id                TEXT NOT NULL,
                    owner_user_id              TEXT NOT NULL,
                    mandate_id                  TEXT NOT NULL,
                    proposal_content_hash      TEXT NOT NULL,
                    mandate_permissions_hash   TEXT NOT NULL,
                    policy_snapshot_hash       TEXT NOT NULL,
                    policy_version             TEXT NOT NULL,
                    policy_decision            TEXT NOT NULL
                        CHECK (policy_decision IN ('ALLOW', 'REQUIRE_APPROVAL')),
                    amount                     TEXT NOT NULL,
                    currency                   TEXT NOT NULL CHECK (length(currency) = 3),
                    review_token_hash          TEXT,
                    binding_hash               TEXT NOT NULL,
                    consented_at               TEXT NOT NULL,
                    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id),
                    FOREIGN KEY (mandate_id) REFERENCES mandates(mandate_id),
                    FOREIGN KEY (review_token_hash) REFERENCES approval_tokens(token_hash),
                    UNIQUE (proposal_id, binding_hash)
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_purchase_consents_owner_proposal "
                "ON purchase_consents(owner_user_id, proposal_id, consented_at)"
            )
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (9, datetime('now'))"
            )
            current_version = 9
            logger.info("Applied migration version 9 (purchase_consents)")

        # Migration 10: bind review tokens and purchase consent to exact auditor evidence.
        if current_version < 10 and target_version >= 10:
            await db.execute(
                "ALTER TABLE approval_tokens ADD COLUMN assessment_binding TEXT"
            )
            await db.execute(
                "ALTER TABLE purchase_consents ADD COLUMN assessment_binding TEXT"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_approval_tokens_assessment_review "
                "ON approval_tokens(proposal_id, assessment_binding, consumed_at)"
            )
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (10, datetime('now'))"
            )
            current_version = 10
            logger.info("Applied migration version 10 (auditor-bound purchase consent)")

        await db.commit()

    logger.info("Database initialisation complete")


async def save_mandate_to_db(
    mandate: Mandate, db: aiosqlite.Connection | None = None
) -> None:
    """Persist a mandate record to SQLite.

    Immutable permissions are preserved; only lifecycle status and signature
    metadata are updated if the record already exists.
    """
    # owner_user_id is set at creation and must remain immutable thereafter.
    # The ON CONFLICT clause deliberately omits owner_user_id so that subsequent
    # upserts (e.g. status updates) cannot overwrite the original ownership record.
    query = """
        INSERT INTO mandates (
            mandate_id, schema_version, status, currency, max_total_budget,
            max_price_per_item, max_item_count, max_quantity_per_item,
            allow_subscriptions, require_user_approval, allowed_merchants,
            allowed_categories, created_at, expires_at, signature,
            signed_at, signature_algorithm, owner_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mandate_id) DO UPDATE SET
            status = excluded.status,
            signature = excluded.signature,
            signed_at = excluded.signed_at,
            signature_algorithm = excluded.signature_algorithm
    """
    params = (
        mandate.mandate_id,
        mandate.schema_version,
        mandate.status.value,
        mandate.currency,
        f"{mandate.max_total_budget:.2f}",
        (
            f"{mandate.max_price_per_item:.2f}"
            if mandate.max_price_per_item is not None
            else None
        ),
        mandate.max_item_count,
        mandate.max_quantity_per_item,
        1 if mandate.allow_subscriptions else 0,
        1 if mandate.require_user_approval else 0,
        json.dumps(mandate.allowed_merchants),
        json.dumps(mandate.allowed_categories),
        mandate.created_at.astimezone(timezone.utc).isoformat(),
        mandate.expires_at.astimezone(timezone.utc).isoformat(),
        mandate.signature,
        (
            mandate.signed_at.astimezone(timezone.utc).isoformat()
            if mandate.signed_at
            else None
        ),
        mandate.signature_algorithm,
        mandate.owner_user_id,
    )
    if db is not None:
        await db.execute(query, params)
        return

    async with get_connection() as conn:
        await conn.execute(query, params)
        await conn.commit()


def _row_to_mandate(row: aiosqlite.Row) -> Mandate:
    """Map a SQLite row to a Mandate model."""
    max_price = row["max_price_per_item"]
    signed_at = row["signed_at"]
    # owner_user_id was added in migration 6; handle rows from pre-migration DBs.
    try:
        owner_user_id = row["owner_user_id"]
    except (IndexError, KeyError):
        owner_user_id = None
    return Mandate(
        mandate_id=row["mandate_id"],
        schema_version=row["schema_version"],
        status=MandateStatus(row["status"]),
        currency=row["currency"],
        max_total_budget=Decimal(row["max_total_budget"]),
        max_price_per_item=Decimal(max_price) if max_price is not None else None,
        max_item_count=row["max_item_count"],
        max_quantity_per_item=row["max_quantity_per_item"],
        allow_subscriptions=bool(row["allow_subscriptions"]),
        require_user_approval=bool(row["require_user_approval"]),
        allowed_merchants=json.loads(row["allowed_merchants"]),
        allowed_categories=json.loads(row["allowed_categories"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        signature=row["signature"],
        signed_at=datetime.fromisoformat(signed_at) if signed_at else None,
        signature_algorithm=row["signature_algorithm"],
        owner_user_id=owner_user_id,
    )


async def get_mandate_from_db(
    mandate_id: str, db: aiosqlite.Connection | None = None
) -> Mandate | None:
    """Retrieve a single mandate by its ID."""
    query = "SELECT * FROM mandates WHERE mandate_id = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (mandate_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_mandate(row)

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (mandate_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_mandate(row)


async def list_mandates_from_db(limit: int = 50) -> list[Mandate]:
    """Retrieve the most recent mandates up to *limit*."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mandates ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_mandate(row) for row in rows]


async def list_mandates_for_owner_from_db(
    owner_user_id: str, limit: int = 100
) -> list[Mandate]:
    """Retrieve only mandates permanently assigned to one authenticated principal."""
    query = (
        "SELECT * FROM mandates WHERE owner_user_id = ? "
        "ORDER BY created_at DESC LIMIT ?"
    )
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (owner_user_id, limit)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_mandate(row) for row in rows]


def _row_to_proposal(row: aiosqlite.Row) -> Proposal:
    """Map a SQLite row to a Proposal model after integrity verification."""
    raw = {
        "proposal_id": row["proposal_id"],
        "schema_version": row["schema_version"],
        "status": row["status"],
        "mandate_id": row["mandate_id"],
        "product_id": row["product_id"],
        "product_name": row["product_name"],
        "merchant": row["merchant"],
        "category": row["category"],
        "quantity": row["quantity"],
        "unit_price": Decimal(row["unit_price"]),
        "currency": row["currency"],
        "subtotal": Decimal(row["subtotal"]),
        "tax": Decimal(row["tax"]),
        "shipping": Decimal(row["shipping"]),
        "other_charges": Decimal(row["other_charges"]),
        "total": Decimal(row["total"]),
        "product_source": row["product_source"],
        "created_at": datetime.fromisoformat(row["created_at"]),
        "idempotency_key": row["idempotency_key"],
        "fingerprint": row["fingerprint"],
        "content_hash": row["content_hash"],
    }
    expected_hash = proposal_content_hash(raw)
    if expected_hash != row["content_hash"]:
        raise StoredProposalTamperedError(
            f"Proposal '{row['proposal_id']}' failed content-hash verification."
        )
    return Proposal.model_validate(raw)


async def insert_proposal_to_db(
    proposal: Proposal, db: aiosqlite.Connection | None = None
) -> None:
    """Insert a new proposal. Commercial terms are write-once."""
    query = """
        INSERT INTO proposals (
            proposal_id, schema_version, status, mandate_id, product_id,
            product_name, merchant, category, quantity, unit_price, currency,
            subtotal, tax, shipping, other_charges, total, product_source,
            created_at, idempotency_key, fingerprint, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        proposal.proposal_id,
        proposal.schema_version,
        proposal.status.value,
        proposal.mandate_id,
        proposal.product_id,
        proposal.product_name,
        proposal.merchant,
        proposal.category,
        proposal.quantity,
        f"{proposal.unit_price:.2f}",
        proposal.currency,
        f"{proposal.subtotal:.2f}",
        f"{proposal.tax:.2f}",
        f"{proposal.shipping:.2f}",
        f"{proposal.other_charges:.2f}",
        f"{proposal.total:.2f}",
        proposal.product_source,
        proposal.created_at.astimezone(timezone.utc).isoformat(),
        proposal.idempotency_key,
        proposal.fingerprint,
        proposal.content_hash,
    )
    if db is not None:
        await db.execute(query, params)
        return

    async with get_connection() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def update_proposal_status_in_db(
    proposal_id: str,
    new_status: ProposalStatus,
    expected_current_status: ProposalStatus,
    db: aiosqlite.Connection | None = None,
) -> bool:
    """Update proposal status only. Returns True if a row was updated."""
    query = """
        UPDATE proposals
        SET status = ?
        WHERE proposal_id = ? AND status = ?
    """
    params = (new_status.value, proposal_id, expected_current_status.value)
    if db is not None:
        cursor = await db.execute(query, params)
        return cursor.rowcount == 1

    async with get_connection() as conn:
        cursor = await conn.execute(query, params)
        await conn.commit()
        return cursor.rowcount == 1


async def get_proposal_from_db(
    proposal_id: str, db: aiosqlite.Connection | None = None
) -> Proposal | None:
    """Retrieve a single proposal by ID."""
    query = "SELECT * FROM proposals WHERE proposal_id = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (proposal_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_proposal(row)

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (proposal_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_proposal(row)


async def get_proposal_for_owner_from_db(
    proposal_id: str, owner_user_id: str
) -> Proposal | None:
    """Retrieve a proposal only when its associated mandate has the given owner."""
    query = (
        "SELECT p.* FROM proposals AS p "
        "JOIN mandates AS m ON m.mandate_id = p.mandate_id "
        "WHERE p.proposal_id = ? AND m.owner_user_id = ?"
    )
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (proposal_id, owner_user_id)) as cursor:
            row = await cursor.fetchone()
            return _row_to_proposal(row) if row is not None else None

async def get_proposal_by_fingerprint(
    fingerprint: str, db: aiosqlite.Connection | None = None
) -> Proposal | None:
    """Retrieve a proposal by duplicate-protection fingerprint."""
    query = "SELECT * FROM proposals WHERE fingerprint = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (fingerprint,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_proposal(row)

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (fingerprint,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_proposal(row)


async def get_proposal_by_idempotency_key(
    idempotency_key: str, db: aiosqlite.Connection | None = None
) -> Proposal | None:
    """Retrieve a proposal by client idempotency key."""
    query = "SELECT * FROM proposals WHERE idempotency_key = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (idempotency_key,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_proposal(row)

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (idempotency_key,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_proposal(row)


async def list_proposals_from_db(
    limit: int = 50,
    mandate_id: str | None = None,
    owner_user_id: str | None = None,
) -> list[Proposal]:
    """Retrieve recent proposals with optional mandate and owner filters."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        filters: list[str] = []
        params_list: list[object] = []
        if owner_user_id is not None:
            filters.append("m.owner_user_id = ?")
            params_list.append(owner_user_id)
        if mandate_id is not None:
            filters.append("p.mandate_id = ?")
            params_list.append(mandate_id)
        where = f"WHERE {' AND '.join(filters)} " if filters else ""
        query = (
            "SELECT p.* FROM proposals AS p "
            "JOIN mandates AS m ON m.mandate_id = p.mandate_id "
            f"{where}ORDER BY p.created_at DESC LIMIT ?"
        )
        params_list.append(limit)
        async with db.execute(query, tuple(params_list)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_proposal(row) for row in rows]


async def get_today_reserved_spend(
    mandate_id: str,
    for_date_iso: str,
    exclude_proposal_id: str | None = None,
    db: aiosqlite.Connection | None = None,
) -> Decimal:
    """Sum total of SUBMITTED, EVALUATED, and APPROVED proposals for mandate today.

    Optionally excludes exclude_proposal_id to prevent double-counting of the proposal
    currently being evaluated.
    """
    if exclude_proposal_id is not None:
        query = """
            SELECT total FROM proposals
            WHERE mandate_id = ?
              AND status IN ('SUBMITTED', 'EVALUATED', 'APPROVED', 'SETTLED')
              AND date(created_at) = date(?)
              AND proposal_id != ?
        """
        params: tuple[object, ...] = (mandate_id, for_date_iso, exclude_proposal_id)
    else:
        query = """
            SELECT total FROM proposals
            WHERE mandate_id = ?
              AND status IN ('SUBMITTED', 'EVALUATED', 'APPROVED', 'SETTLED')
              AND date(created_at) = date(?)
        """
        params = (mandate_id, for_date_iso)

    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return sum((Decimal(row["total"]) for row in rows), Decimal("0.00"))

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return sum((Decimal(row["total"]) for row in rows), Decimal("0.00"))


async def get_cumulative_mandate_spent(
    mandate_id: str,
    exclude_proposal_id: str | None = None,
    db: aiosqlite.Connection | None = None,
) -> Decimal:
    """Sum total of approved/settled proposals for mandate.

    Optionally excludes exclude_proposal_id to prevent double-counting.
    """
    if exclude_proposal_id is not None:
        query = """
            SELECT total FROM proposals
            WHERE mandate_id = ?
              AND status IN ('EVALUATED', 'APPROVED', 'SETTLED')
              AND proposal_id != ?
        """
        params: tuple[object, ...] = (mandate_id, exclude_proposal_id)
    else:
        query = """
            SELECT total FROM proposals
            WHERE mandate_id = ?
              AND status IN ('EVALUATED', 'APPROVED', 'SETTLED')
        """
        params = (mandate_id,)

    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return sum((Decimal(row["total"]) for row in rows), Decimal("0.00"))

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return sum((Decimal(row["total"]) for row in rows), Decimal("0.00"))


def hash_approval_token(token: str) -> str:
    """Compute deterministic SHA-256 hash of approval token.

    Raw approval tokens are never stored in the database.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def record_approval_token_in_db(
    token_hash: str,
    proposal_id: str,
    owner_user_id: str,
    content_hash: str,
    assessment_binding: str,
    expires_at_iso: str,
    created_at_iso: str,
    db: aiosqlite.Connection | None = None,
) -> None:
    """Persist an issued approval token hash and metadata before use."""
    query = """
        INSERT INTO approval_tokens (
            token_hash, proposal_id, owner_user_id, content_hash, assessment_binding,
            expires_at, created_at, consumed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
    """
    params = (token_hash, proposal_id, owner_user_id, content_hash,
              assessment_binding, expires_at_iso, created_at_iso)
    if db is not None:
        await db.execute(query, params)
        return

    async with get_connection() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def consume_approval_token_in_db(
    token_hash: str,
    proposal_id: str,
    owner_user_id: str,
    content_hash: str,
    assessment_binding: str,
    consumed_at_iso: str,
    db: aiosqlite.Connection | None = None,
) -> tuple[bool, str]:
    """Atomically consume an approval token, preventing replay or concurrent use.

    Returns:
        Tuple of (success, reason).
    """
    query_lookup = "SELECT * FROM approval_tokens WHERE token_hash = ?"

    async def _execute_consume(conn: aiosqlite.Connection) -> tuple[bool, str]:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query_lookup, (token_hash,)) as cursor:
            row = await cursor.fetchone()

        if row is None:
            # Token was not pre-registered; insert as consumed atomically
            async with conn.execute(
                "SELECT proposal_id FROM proposals WHERE proposal_id = ?",
                (proposal_id,),
            ) as p_cursor:
                if await p_cursor.fetchone() is None:
                    return False, f"Proposal '{proposal_id}' not found."

            try:
                await conn.execute(
                    """
                    INSERT INTO approval_tokens (
                        token_hash, proposal_id, owner_user_id, content_hash,
                        assessment_binding,
                        expires_at, created_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token_hash,
                        proposal_id,
                        owner_user_id,
                        content_hash,
                        assessment_binding,
                        consumed_at_iso,
                        consumed_at_iso,
                        consumed_at_iso,
                    ),
                )
                return True, "Approval token consumed successfully."
            except aiosqlite.IntegrityError:
                return False, "Approval token already consumed (concurrent replay detected)."

        if row["proposal_id"] != proposal_id:
            return False, f"Approval token is not bound to proposal '{proposal_id}'."

        if row["consumed_at"] is not None:
            return (
                False,
                f"Approval token has already been consumed at {row['consumed_at']} "
                "(replay detected).",
            )

        if row["expires_at"] < consumed_at_iso:
            return False, f"Approval token expired at {row['expires_at']}."

        # An unbound legacy row is updated only after the caller verifies the HMAC
        # against the exact current assessment and owner/content values. Persist
        # all bindings in the same atomic update that marks the token consumed.
        if row["assessment_binding"] is None:
            if (row["owner_user_id"] is not None and row["owner_user_id"] != owner_user_id) or (
                row["content_hash"] is not None and row["content_hash"] != content_hash
            ):
                return False, "Approval token review binding does not match current authorization."
            cursor = await conn.execute(
                """
                UPDATE approval_tokens
                SET owner_user_id = ?, content_hash = ?, assessment_binding = ?, consumed_at = ?
                WHERE token_hash = ? AND consumed_at IS NULL
                  AND assessment_binding IS NULL
                """,
                (owner_user_id, content_hash, assessment_binding, consumed_at_iso, token_hash),
            )
            if cursor.rowcount == 1:
                return True, "Approval token consumed successfully."
            return False, "Approval token was changed concurrently; retry review."

        if (row["owner_user_id"] != owner_user_id
                or row["content_hash"] != content_hash
                or row["assessment_binding"] != assessment_binding):
            return False, "Approval token review binding does not match current authorization."

        cursor = await conn.execute(
            """
            UPDATE approval_tokens
            SET consumed_at = ?
            WHERE token_hash = ? AND consumed_at IS NULL
            """,
            (consumed_at_iso, token_hash),
        )
        if cursor.rowcount == 1:
            return True, "Approval token consumed successfully."
        return False, "Approval token already consumed concurrently (replay detected)."

    if db is not None:
        return await _execute_consume(db)

    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            success, reason = await _execute_consume(conn)
            if success:
                await conn.commit()
            else:
                await conn.rollback()
            return success, reason
        except Exception:
            await conn.rollback()
            raise


def _parse_iso_utc(val: str | None) -> datetime | None:
    """Parse an ISO timestamp string, ensuring it is timezone-aware UTC."""
    if not val:
        return None
    dt = datetime.fromisoformat(val)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_to_kill_switch_status(row: aiosqlite.Row) -> KillSwitchStatus:
    """Map a SQLite row to KillSwitchStatus."""
    return KillSwitchStatus(
        kill_switch_active=bool(row["kill_switch_active"]),
        engaged_at=_parse_iso_utc(row["engaged_at"]),
        engaged_by=row["engaged_by"],
        reason=row["reason"],
        updated_at=_parse_iso_utc(row["updated_at"]),
    )


def _row_to_audit_entry(row: aiosqlite.Row) -> AuditLogEntry:
    """Map a SQLite row to AuditLogEntry."""
    return AuditLogEntry(
        sequence_number=row["sequence_number"],
        timestamp=_parse_iso_utc(row["timestamp"]),
        event_type=row["event_type"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        actor=row["actor"],
        payload=json.loads(row["payload_json"]),
        prev_hash=row["prev_hash"],
        entry_hash=row["entry_hash"],
    )


def _row_to_checkpoint(row: aiosqlite.Row) -> AuditCheckpoint:
    """Map a SQLite row to AuditCheckpoint."""
    return AuditCheckpoint(
        checkpoint_id=row["checkpoint_id"],
        sequence_number=row["sequence_number"],
        entry_hash=row["entry_hash"],
        sealed_at=_parse_iso_utc(row["sealed_at"]),
        sealed_by=row["sealed_by"],
        checkpoint_sig=row["checkpoint_sig"],
    )


async def is_kill_switch_active(db: aiosqlite.Connection | None = None) -> bool:
    """Return True if emergency kill switch is currently engaged."""
    query = "SELECT kill_switch_active FROM system_controls WHERE control_id = 1"
    if db is not None:
        async with db.execute(query) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row is not None else False

    async with get_connection() as conn:
        async with conn.execute(query) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row is not None else False


async def get_kill_switch_status(
    db: aiosqlite.Connection | None = None,
) -> KillSwitchStatus:
    """Retrieve full system controls status."""
    query = "SELECT * FROM system_controls WHERE control_id = 1"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query) as cursor:
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("system_controls table is not initialized.")
            return _row_to_kill_switch_status(row)

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query) as cursor:
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("system_controls table is not initialized.")
            return _row_to_kill_switch_status(row)


async def set_kill_switch_state(
    active: bool,
    engaged_by: str | None,
    reason: str | None,
    now: datetime | None = None,
    db: aiosqlite.Connection | None = None,
) -> KillSwitchStatus:
    """Update kill switch state in system_controls."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now_utc.isoformat()
    engaged_at_iso = now_iso if active else None

    query = """
        UPDATE system_controls
        SET kill_switch_active = ?,
            engaged_at = ?,
            engaged_by = ?,
            reason = ?,
            updated_at = ?
        WHERE control_id = 1
    """
    params = (
        1 if active else 0,
        engaged_at_iso,
        engaged_by,
        reason,
        now_iso,
    )

    if db is not None:
        await db.execute(query, params)
        return await get_kill_switch_status(db=db)

    async with get_connection() as conn:
        await conn.execute(query, params)
        await conn.commit()
        return await get_kill_switch_status(db=conn)


async def get_latest_audit_entry(
    db: aiosqlite.Connection | None = None,
) -> AuditLogEntry | None:
    """Retrieve the latest record at the tip of the audit chain."""
    query = "SELECT * FROM audit_logs ORDER BY sequence_number DESC LIMIT 1"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query) as cursor:
            row = await cursor.fetchone()
            return _row_to_audit_entry(row) if row is not None else None

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query) as cursor:
            row = await cursor.fetchone()
            return _row_to_audit_entry(row) if row is not None else None


async def _execute_insert_audit_log(
    conn: aiosqlite.Connection, entry: AuditLogEntry
) -> None:
    """Validate and insert an audit log entry within an open connection."""
    computed_hash = entry.compute_canonical_hash()
    if entry.entry_hash != computed_hash:
        raise ValueError(
            f"entry_hash mismatch for sequence {entry.sequence_number}: "
            f"stored entry_hash starts with '{entry.entry_hash[:12]}...', "
            f"but canonical recompute gives '{computed_hash[:12]}...'. "
            "Ensure entry_hash is computed via canonical_audit_hash(entry.to_envelope()) "
            "and that the envelope was not modified after hashing."
        )

    # Validate sequence contiguity and prev_hash linking
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT sequence_number, entry_hash FROM audit_logs "
        "ORDER BY sequence_number DESC LIMIT 1"
    ) as cursor:
        last_row = await cursor.fetchone()

    if last_row is None:
        if entry.sequence_number != 1:
            raise ValueError(
                f"First audit entry must have sequence_number=1, got {entry.sequence_number}."
            )
        if entry.prev_hash != "0" * 64:
            raise ValueError(
                f"Genesis audit entry must have prev_hash='{'0'*64}', got '{entry.prev_hash}'."
            )
    else:
        expected_seq = last_row["sequence_number"] + 1
        expected_prev_hash = last_row["entry_hash"]
        if entry.sequence_number != expected_seq:
            raise ValueError(
                f"Non-contiguous sequence_number for audit log: expected {expected_seq}, "
                f"got {entry.sequence_number}."
            )
        if entry.prev_hash != expected_prev_hash:
            raise ValueError(
                f"Invalid prev_hash for sequence {entry.sequence_number}: expected preceding "
                f"entry_hash '{expected_prev_hash}', got '{entry.prev_hash}'."
            )

    query = """
        INSERT INTO audit_logs (
            sequence_number, timestamp, event_type, entity_type,
            entity_id, actor, payload_json, prev_hash, entry_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        entry.sequence_number,
        entry.timestamp.astimezone(timezone.utc).isoformat(),
        entry.event_type,
        entry.entity_type,
        entry.entity_id,
        entry.actor,
        json.dumps(entry.payload, sort_keys=True, separators=(",", ":")),
        entry.prev_hash,
        entry.entry_hash,
    )
    await conn.execute(query, params)


async def insert_audit_log_entry(
    entry: AuditLogEntry,
    db: aiosqlite.Connection | None = None,
) -> None:
    """Insert a verified audit record into audit_logs.

    Guarantees:
    - Canonical hash recomputation and verification.
    - Contiguous sequence allocation and strict prev_hash chain linking.
    - Atomic execution using SQLite BEGIN IMMEDIATE when managing its own connection.

    Raises:
        ValueError: If entry_hash, sequence_number, or prev_hash fails verification.
    """
    if db is not None:
        await _execute_insert_audit_log(db, entry)
        return

    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await _execute_insert_audit_log(conn, entry)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def list_audit_log_entries(
    limit: int = 50,
    offset: int = 0,
    entity_type: str | None = None,
    entity_id: str | None = None,
    event_type: str | None = None,
    db: aiosqlite.Connection | None = None,
) -> tuple[list[AuditLogEntry], int]:
    """List audit log records with optional filtering and pagination."""
    conditions: list[str] = []
    params: list[Any] = []

    if entity_type is not None:
        conditions.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        conditions.append("entity_id = ?")
        params.append(entity_id)
    if event_type is not None:
        conditions.append("event_type = ?")
        params.append(event_type)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    count_query = f"SELECT COUNT(*) FROM audit_logs {where_clause}"
    select_query = f"""
        SELECT * FROM audit_logs
        {where_clause}
        ORDER BY sequence_number ASC
        LIMIT ? OFFSET ?
    """

    async def _fetch(conn: aiosqlite.Connection) -> tuple[list[AuditLogEntry], int]:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(count_query, params) as c_cursor:
            total = (await c_cursor.fetchone())[0]

        fetch_params = tuple(params + [limit, offset])
        async with conn.execute(select_query, fetch_params) as s_cursor:
            rows = await s_cursor.fetchall()
            return [_row_to_audit_entry(r) for r in rows], total

    if db is not None:
        return await _fetch(db)

    async with get_connection() as conn:
        return await _fetch(conn)


async def get_audit_checkpoints(
    db: aiosqlite.Connection | None = None,
) -> list[AuditCheckpoint]:
    """Retrieve all audit checkpoints in ascending order."""
    query = "SELECT * FROM audit_checkpoints ORDER BY checkpoint_id ASC"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_checkpoint(r) for r in rows]

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_checkpoint(r) for r in rows]


async def insert_audit_checkpoint(
    checkpoint: AuditCheckpoint,
    db: aiosqlite.Connection | None = None,
) -> None:
    """Insert a signed checkpoint into audit_checkpoints."""
    query = """
        INSERT INTO audit_checkpoints (
            sequence_number, entry_hash, sealed_at, sealed_by, checkpoint_sig
        ) VALUES (?, ?, ?, ?, ?)
    """
    params = (
        checkpoint.sequence_number,
        checkpoint.entry_hash,
        checkpoint.sealed_at.astimezone(timezone.utc).isoformat(),
        checkpoint.sealed_by,
        checkpoint.checkpoint_sig,
    )
    if db is not None:
        await db.execute(query, params)
        return

    async with get_connection() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def get_audit_entry_by_sequence(
    sequence_number: int,
    db: aiosqlite.Connection | None = None,
) -> AuditLogEntry | None:
    """Retrieve a single audit log entry by its sequence number."""
    query = "SELECT * FROM audit_logs WHERE sequence_number = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (sequence_number,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_audit_entry(row) if row is not None else None

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (sequence_number,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_audit_entry(row) if row is not None else None


async def get_all_audit_entries(
    db: aiosqlite.Connection | None = None,
) -> list[AuditLogEntry]:
    """Retrieve all audit log entries in ascending sequence order."""
    query = "SELECT * FROM audit_logs ORDER BY sequence_number ASC"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_audit_entry(r) for r in rows]

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_audit_entry(r) for r in rows]


async def get_checkpoint_by_sequence(
    sequence_number: int,
    db: aiosqlite.Connection | None = None,
) -> AuditCheckpoint | None:
    """Retrieve a checkpoint by its target sequence number."""
    query = "SELECT * FROM audit_checkpoints WHERE sequence_number = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (sequence_number,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_checkpoint(row) if row is not None else None

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (sequence_number,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_checkpoint(row) if row is not None else None


async def get_latest_audit_checkpoint(
    db: aiosqlite.Connection | None = None,
) -> AuditCheckpoint | None:
    """Retrieve the latest checkpoint anchor with the highest sequence number."""
    query = "SELECT * FROM audit_checkpoints ORDER BY sequence_number DESC LIMIT 1"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query) as cursor:
            row = await cursor.fetchone()
            return _row_to_checkpoint(row) if row is not None else None

    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query) as cursor:
            row = await cursor.fetchone()
            return _row_to_checkpoint(row) if row is not None else None



async def create_session_in_db(
    token_hash: str,
    user_id: str,
    created_at_iso: str,
    expires_at_iso: str,
    db: aiosqlite.Connection | None = None,
) -> None:
    """Persist a new session record. Only the token hash is stored.

    Args:
        token_hash: SHA-256 hex digest of the raw bearer token.
        user_id: Principal identifier bound to this session.
        created_at_iso: ISO-8601 UTC creation timestamp.
        expires_at_iso: ISO-8601 UTC expiration timestamp.
        db: Optional existing connection. If None a new connection is opened.
    """
    query = """
        INSERT INTO user_sessions (session_token_hash, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
    """
    params = (token_hash, user_id, created_at_iso, expires_at_iso)
    if db is not None:
        await db.execute(query, params)
        return
    async with get_connection() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def get_session_by_hash(
    token_hash: str,
    db: aiosqlite.Connection | None = None,
) -> dict | None:
    """Retrieve session row by token hash. Returns None if not found.

    Args:
        token_hash: SHA-256 hex digest of the raw bearer token.
        db: Optional existing connection. If None a new connection is opened.

    Returns:
        A plain dict of the session row, or None if no matching row exists.
    """
    query = "SELECT * FROM user_sessions WHERE session_token_hash = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (token_hash,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (token_hash,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None


async def revoke_session_in_db(
    token_hash: str,
    revoked_at_iso: str,
    db: aiosqlite.Connection | None = None,
) -> bool:
    """Mark a session as revoked. Returns True if a row was updated.

    Args:
        token_hash: SHA-256 hex digest of the raw bearer token.
        revoked_at_iso: ISO-8601 UTC revocation timestamp.
        db: Optional existing connection. If None a new connection is opened.

    Returns:
        True if the session row was found and updated, False otherwise.
    """
    query = """
        UPDATE user_sessions
        SET revoked_at = ?
        WHERE session_token_hash = ? AND revoked_at IS NULL
    """
    params = (revoked_at_iso, token_hash)
    if db is not None:
        cursor = await db.execute(query, params)
        return cursor.rowcount > 0
    async with get_connection() as conn:
        cursor = await conn.execute(query, params)
        await conn.commit()
        return cursor.rowcount > 0


def _row_to_paypal_order(row: aiosqlite.Row) -> PayPalOrderRecord:
    """Map a SQLite row to a PayPalOrderRecord model."""
    captured_at = row["captured_at"]
    return PayPalOrderRecord(
        order_id=row["order_id"],
        proposal_id=row["proposal_id"],
        mandate_id=row["mandate_id"],
        owner_user_id=row["owner_user_id"],
        status=PayPalOrderStatus(row["status"]),
        currency=row["currency"],
        total_amount=Decimal(row["total_amount"]),
        subtotal=Decimal(row["subtotal"]),
        tax=Decimal(row["tax"]),
        shipping=Decimal(row["shipping"]),
        other_charges=Decimal(row["other_charges"]),
        approval_url=row["approval_url"],
        paypal_request_id=row["paypal_request_id"],
        capture_id=row["capture_id"],
        capture_request_id=row["capture_request_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        captured_at=datetime.fromisoformat(captured_at) if captured_at else None,
    )


async def insert_paypal_order_in_db(
    order: PayPalOrderRecord, db: aiosqlite.Connection | None = None
) -> None:
    """Insert a new PayPal order record into the database."""
    query = """
        INSERT INTO paypal_orders (
            order_id, proposal_id, mandate_id, owner_user_id, status,
            currency, total_amount, subtotal, tax, shipping, other_charges,
            approval_url, paypal_request_id, capture_id, capture_request_id,
            created_at, updated_at, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        order.order_id,
        order.proposal_id,
        order.mandate_id,
        order.owner_user_id,
        order.status.value,
        order.currency,
        f"{order.total_amount:.2f}",
        f"{order.subtotal:.2f}",
        f"{order.tax:.2f}",
        f"{order.shipping:.2f}",
        f"{order.other_charges:.2f}",
        order.approval_url,
        order.paypal_request_id,
        order.capture_id,
        order.capture_request_id,
        order.created_at.astimezone(timezone.utc).isoformat(),
        order.updated_at.astimezone(timezone.utc).isoformat(),
        order.captured_at.astimezone(timezone.utc).isoformat() if order.captured_at else None,
    )
    if db is not None:
        await db.execute(query, params)
        return
    async with get_connection() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def get_paypal_order_from_db(
    order_id: str, db: aiosqlite.Connection | None = None
) -> PayPalOrderRecord | None:
    """Retrieve a PayPal order by its PayPal order_id."""
    query = "SELECT * FROM paypal_orders WHERE order_id = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (order_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_paypal_order(row) if row is not None else None
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (order_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_paypal_order(row) if row is not None else None


async def get_paypal_order_by_proposal_from_db(
    proposal_id: str, db: aiosqlite.Connection | None = None
) -> PayPalOrderRecord | None:
    """Retrieve an existing PayPal order associated with a proposal."""
    query = "SELECT * FROM paypal_orders WHERE proposal_id = ?"
    if db is not None:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (proposal_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_paypal_order(row) if row is not None else None
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, (proposal_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_paypal_order(row) if row is not None else None


async def update_paypal_order_captured_in_db(
    order_id: str,
    capture_id: str,
    capture_request_id: str,
    captured_at_iso: str,
    updated_at_iso: str,
    db: aiosqlite.Connection | None = None,
) -> bool:
    """Atomically record capture for a PayPal order if not already captured."""
    query = """
        UPDATE paypal_orders
        SET status = ?,
            capture_id = ?,
            capture_request_id = ?,
            captured_at = ?,
            updated_at = ?
        WHERE order_id = ? AND status != ?
    """
    params = (
        PayPalOrderStatus.COMPLETED.value,
        capture_id,
        capture_request_id,
        captured_at_iso,
        updated_at_iso,
        order_id,
        PayPalOrderStatus.COMPLETED.value,
    )
    if db is not None:
        cursor = await db.execute(query, params)
        return cursor.rowcount > 0
    async with get_connection() as conn:
        cursor = await conn.execute(query, params)
        await conn.commit()
        return cursor.rowcount > 0


async def update_paypal_order_status_in_db(
    order_id: str,
    status: PayPalOrderStatus,
    updated_at_iso: str,
    db: aiosqlite.Connection | None = None,
) -> bool:
    """Update status of a PayPal order."""
    query = (
        "UPDATE paypal_orders SET status = ?, updated_at = ? "
        "WHERE order_id = ? AND status != ?"
    )
    params = (
        status.value,
        updated_at_iso,
        order_id,
        PayPalOrderStatus.COMPLETED.value,
    )
    if db is not None:
        cursor = await db.execute(query, params)
        return cursor.rowcount > 0
    async with get_connection() as conn:
        cursor = await conn.execute(query, params)
        await conn.commit()
        return cursor.rowcount > 0
