"""Tests for SQLite database path resolution and initialization."""

import tempfile
from pathlib import Path

import pytest

from app.core.config import BACKEND_DIR, ROOT_DIR
from app.db.database import get_db_path, init_db


def test_database_path_from_backend_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify relative database path resolution when executed from backend directory."""
    monkeypatch.chdir(BACKEND_DIR)
    resolved = get_db_path("sqlite:///./leash.db")
    expected = str((BACKEND_DIR / "leash.db").resolve())
    assert resolved == expected


def test_database_path_from_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify relative database path resolution when executed from repository root."""
    monkeypatch.chdir(ROOT_DIR)
    resolved = get_db_path("sqlite:///./leash.db")
    expected = str((BACKEND_DIR / "leash.db").resolve())
    assert resolved == expected


def test_database_path_absolute_custom_path() -> None:
    """Verify absolute custom paths remain unchanged."""
    abs_path = "/tmp/custom_test_leash.db"
    resolved = get_db_path(f"sqlite:///{abs_path}")
    assert resolved == abs_path


def test_database_path_in_memory() -> None:
    """Verify in-memory database specifiers remain unchanged."""
    assert get_db_path("sqlite:///:memory:") == ":memory:"
    assert get_db_path(":memory:") == ":memory:"


@pytest.mark.asyncio
async def test_init_db_applies_mandates_schema_version() -> None:
    """Verify migration 2 creates the mandates table and records schema version 2."""
    import aiosqlite

    from app.db.database import get_db_path

    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 2

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mandates'"
        )
        table = await cursor.fetchone()
        assert table is not None
        assert table[0] == "mandates"


@pytest.mark.asyncio
async def test_init_db_applies_proposals_schema_version() -> None:
    """Verify migration 3 creates the proposals table without dropping mandates."""
    import aiosqlite

    from app.db.database import get_db_path

    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 3

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='proposals'"
        )
        table = await cursor.fetchone()
        assert table is not None
        assert table[0] == "proposals"

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mandates'"
        )
        mandates = await cursor.fetchone()
        assert mandates is not None


@pytest.mark.asyncio
async def test_save_mandate_does_not_overwrite_permissions() -> None:
    """Verify ON CONFLICT updates keep permission columns immutable."""
    from decimal import Decimal
    from datetime import datetime, timedelta, timezone

    from app.db.database import get_mandate_from_db, save_mandate_to_db
    from app.schemas.mandate import MandateCreate
    from app.services.mandate_service import create_draft_mandate

    payload = MandateCreate(
        max_total_budget=Decimal("40.00"),
        currency="USD",
        allowed_merchants=["amazon.com"],
        allowed_categories=["books"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    draft = await create_draft_mandate(payload)
    draft.allowed_merchants = ["evil.com"]
    draft.max_total_budget = Decimal("999.00")
    await save_mandate_to_db(draft)

    loaded = await get_mandate_from_db(draft.mandate_id)
    assert loaded is not None
    assert loaded.allowed_merchants == ["amazon.com"]
    assert loaded.max_total_budget == Decimal("40.00")


@pytest.mark.asyncio
async def test_init_db_creates_parent_directory() -> None:
    """Verify init_db creates parent directories for subfolder database paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nested_db = Path(tmpdir) / "nested" / "sub" / "test.db"
        assert not nested_db.parent.exists()

        path = get_db_path(str(nested_db))
        assert path == str(nested_db)

        from app.core.config import settings
        original_url = settings.DATABASE_URL
        try:
            settings.DATABASE_URL = str(nested_db)
            await init_db()
            assert nested_db.parent.exists()
            assert nested_db.exists()
        finally:
            settings.DATABASE_URL = original_url
