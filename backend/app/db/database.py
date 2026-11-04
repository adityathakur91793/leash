"""Lightweight async SQLite database helpers using aiosqlite."""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import aiosqlite

from app.core.config import BACKEND_DIR, settings
from app.schemas.mandate import Mandate, MandateStatus

logger = logging.getLogger(__name__)


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


async def init_db() -> None:
    """Create database tables and apply incremental schema migrations."""
    db_path: str = get_db_path()

    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    logger.info("Initialising database at %s", db_path)

    async with aiosqlite.connect(db_path) as db:
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
            logger.info("Applied migration version 2 (mandates)")

        await db.commit()

    logger.info("Database initialisation complete")


async def save_mandate_to_db(mandate: Mandate) -> None:
    """Persist a mandate record to SQLite.

    Immutable permissions are preserved; only lifecycle status and signature
    metadata are updated if the record already exists.
    """
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
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
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
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
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mandates ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_mandate(row) for row in rows]
