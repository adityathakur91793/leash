"""Lightweight async SQLite database helpers using aiosqlite."""

import logging
from pathlib import Path

import aiosqlite

from app.core.config import BACKEND_DIR, settings

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
    """Create the database file and bootstrap the ``schema_version`` table.

    Inserts version **1** when the table is first created.
    """
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

        cursor = await db.execute("SELECT COUNT(*) FROM schema_version")
        row = await cursor.fetchone()

        if row is not None and row[0] == 0:
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (1, datetime('now'))"
            )
            logger.info("Inserted initial schema version 1")

        await db.commit()

    logger.info("Database initialisation complete")
