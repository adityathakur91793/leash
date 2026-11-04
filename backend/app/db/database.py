"""Lightweight async SQLite database helpers using aiosqlite."""

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
            logger.info("Applied migration version 3 (proposals)")

        await db.commit()

    logger.info("Database initialisation complete")


async def save_mandate_to_db(mandate: Mandate) -> None:
    """Persist a mandate record to SQLite.

    Immutable permissions are preserved; only lifecycle status and signature
    metadata are updated if the record already exists.
    """
    async with get_connection() as db:
        await db.execute(
            """
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
            """,
            (
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
            ),
        )
        await db.commit()


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


async def get_mandate_from_db(mandate_id: str) -> Mandate | None:
    """Retrieve a single mandate by its ID."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mandates WHERE mandate_id = ?", (mandate_id,)
        ) as cursor:
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


async def insert_proposal_to_db(proposal: Proposal) -> None:
    """Insert a new proposal. Commercial terms are write-once."""
    async with get_connection() as db:
        await db.execute(
            """
            INSERT INTO proposals (
                proposal_id, schema_version, status, mandate_id, product_id,
                product_name, merchant, category, quantity, unit_price, currency,
                subtotal, tax, shipping, other_charges, total, product_source,
                created_at, idempotency_key, fingerprint, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
            ),
        )
        await db.commit()


async def update_proposal_status_in_db(
    proposal_id: str,
    new_status: ProposalStatus,
    expected_current_status: ProposalStatus,
) -> bool:
    """Update proposal status only. Returns True if a row was updated."""
    async with get_connection() as db:
        cursor = await db.execute(
            """
            UPDATE proposals
            SET status = ?
            WHERE proposal_id = ? AND status = ?
            """,
            (new_status.value, proposal_id, expected_current_status.value),
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_proposal_from_db(proposal_id: str) -> Proposal | None:
    """Retrieve a single proposal by ID."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_proposal(row)


async def get_proposal_by_fingerprint(fingerprint: str) -> Proposal | None:
    """Retrieve a proposal by duplicate-protection fingerprint."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM proposals WHERE fingerprint = ?", (fingerprint,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_proposal(row)


async def get_proposal_by_idempotency_key(idempotency_key: str) -> Proposal | None:
    """Retrieve a proposal by client idempotency key."""
    async with get_connection() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM proposals WHERE idempotency_key = ?", (idempotency_key,)
        ) as cursor:
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
