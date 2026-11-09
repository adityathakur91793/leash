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
async def test_init_db_applies_audit_logs_schema_version() -> None:
    """Verify migration 5 creates audit_logs, audit_checkpoints, and system_controls."""
    import aiosqlite

    from app.db.database import get_db_path

    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 5

        for table in ("audit_logs", "audit_checkpoints", "system_controls"):
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            res = await cursor.fetchone()
            assert res is not None, f"Table {table} must be created in migration 5."


@pytest.mark.asyncio
async def test_migration_7_creates_paypal_orders_schema() -> None:
    """Migration 7 creates durable, uniquely proposal-bound PayPal orders."""
    import aiosqlite

    from app.db.database import get_db_path

    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        assert (await cursor.fetchone())[0] >= 7
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='paypal_orders'"
        )
        assert (await cursor.fetchone())[0] == "paypal_orders"
        cursor = await db.execute("PRAGMA index_list(paypal_orders)")
        indexes = {row[1] for row in await cursor.fetchall()}
        assert "idx_paypal_orders_proposal" in indexes


@pytest.mark.asyncio
async def test_migration_11_creates_durable_paypal_order_attempts() -> None:
    import aiosqlite
    from app.db.database import get_db_path

    async with aiosqlite.connect(get_db_path()) as db:
        version = await (await db.execute(
            "SELECT MAX(version) FROM schema_version"
        )).fetchone()
        assert version[0] >= 11
        table = await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("paypal_order_attempts",),
        )).fetchone()
        assert table == ("paypal_order_attempts",)
        indexes = {row[1] for row in await (await db.execute(
            "PRAGMA index_list(paypal_order_attempts)"
        )).fetchall()}
        assert "idx_paypal_attempt_owner" in indexes


@pytest.mark.asyncio
async def test_paypal_order_reconciliation_cannot_demote_completed_order() -> None:
    """A late timeout handler must not overwrite concurrent capture success."""
    import aiosqlite
    from datetime import datetime, timezone

    from app.db.database import update_paypal_order_status_in_db
    from app.schemas.payment import PayPalOrderStatus

    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE paypal_orders (order_id TEXT, status TEXT, updated_at TEXT)"
        )
        await db.execute(
            "INSERT INTO paypal_orders VALUES (?, ?, ?)",
            ("order-1", PayPalOrderStatus.COMPLETED.value, datetime.now(timezone.utc).isoformat()),
        )
        changed = await update_paypal_order_status_in_db(
            "order-1",
            PayPalOrderStatus.RECONCILIATION_REQUIRED,
            datetime.now(timezone.utc).isoformat(),
            db=db,
        )
        cursor = await db.execute("SELECT status FROM paypal_orders WHERE order_id = ?", ("order-1",))
        row = await cursor.fetchone()
        assert changed is False
        assert row[0] == PayPalOrderStatus.COMPLETED.value


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
