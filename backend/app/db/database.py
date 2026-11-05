"""Lightweight async SQLite database helpers using aiosqlite."""

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import aiosqlite

from app.core.config import BACKEND_DIR, settings
from app.core.security import proposal_content_hash
from app.schemas.mandate import Mandate, MandateStatus
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


async def init_db() -> None:
    """Create database tables and apply incremental schema migrations."""
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
        if current_version < 1:
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (1, datetime('now'))"
            )
            current_version = 1
            logger.info("Applied migration version 1")

        # Migration 2: Mandates table (Phase 2A)
        if current_version < 2:
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
        if current_version < 3:
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
        if current_version < 4:
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

        await db.commit()

    logger.info("Database initialisation complete")


async def save_mandate_to_db(
    mandate: Mandate, db: aiosqlite.Connection | None = None
) -> None:
    """Persist a mandate record to SQLite.

    Immutable permissions are preserved; only lifecycle status and signature
    metadata are updated if the record already exists.
    """
    query = """
        INSERT INTO mandates (
            mandate_id, schema_version, status, currency, max_total_budget,
            max_price_per_item, max_item_count, max_quantity_per_item,
            allow_subscriptions, require_user_approval, allowed_merchants,
            allowed_categories, created_at, expires_at, signature,
            signed_at, signature_algorithm
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    limit: int = 50, mandate_id: str | None = None
) -> list[Proposal]:
    """Retrieve recent proposals, optionally filtered by mandate."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        if mandate_id is None:
            query = "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?"
            params: tuple[object, ...] = (limit,)
        else:
            query = (
                "SELECT * FROM proposals WHERE mandate_id = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params = (mandate_id, limit)
        async with db.execute(query, params) as cursor:
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
              AND status IN ('SUBMITTED', 'EVALUATED', 'APPROVED')
              AND date(created_at) = date(?)
              AND proposal_id != ?
        """
        params: tuple[object, ...] = (mandate_id, for_date_iso, exclude_proposal_id)
    else:
        query = """
            SELECT total FROM proposals
            WHERE mandate_id = ?
              AND status IN ('SUBMITTED', 'EVALUATED', 'APPROVED')
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
              AND status IN ('EVALUATED', 'APPROVED')
              AND proposal_id != ?
        """
        params: tuple[object, ...] = (mandate_id, exclude_proposal_id)
    else:
        query = """
            SELECT total FROM proposals
            WHERE mandate_id = ?
              AND status IN ('EVALUATED', 'APPROVED')
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
    expires_at_iso: str,
    created_at_iso: str,
    db: aiosqlite.Connection | None = None,
) -> None:
    """Persist an issued approval token hash and metadata before use."""
    query = """
        INSERT INTO approval_tokens (
            token_hash, proposal_id, expires_at, created_at, consumed_at
        ) VALUES (?, ?, ?, ?, NULL)
    """
    params = (token_hash, proposal_id, expires_at_iso, created_at_iso)
    if db is not None:
        await db.execute(query, params)
        return

    async with get_connection() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def consume_approval_token_in_db(
    token_hash: str,
    proposal_id: str,
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
                        token_hash, proposal_id, expires_at, created_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        token_hash,
                        proposal_id,
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
                f"Approval token has already been consumed at {row['consumed_at']} (replay detected).",
            )

        if row["expires_at"] < consumed_at_iso:
            return False, f"Approval token expired at {row['expires_at']}."

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
