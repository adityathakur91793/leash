"""Shared test fixtures for the LEASH backend test suite."""

import os
import tempfile
from collections.abc import AsyncIterator

import aiosqlite
import httpx
import pytest

# Point the database at a temporary file BEFORE importing the app so that
# the Settings singleton picks up the override.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ.setdefault(
    "MANDATE_SIGNING_KEY",
    "leash-test-secret-signing-key-32-bytes-minimum!!",
)
os.environ.setdefault(
    "ADMIN_API_KEY",
    "leash-test-admin-key-32-bytes-minimum-secret!!",
)
os.environ.setdefault(
    "PAYPAL_CLIENT_ID",
    "mock-paypal-client-id",
)
os.environ.setdefault(
    "PAYPAL_CLIENT_SECRET",
    "mock-paypal-client-secret",
)

from app.db.database import get_db_path, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
async def init_test_db() -> AsyncIterator[None]:
    """Ensure the temporary test database schema is initialized before tests run."""
    await init_db()
    yield
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM paypal_orders")
        await db.execute("DELETE FROM audit_checkpoints")
        await db.execute("DELETE FROM audit_logs")
        await db.execute("DELETE FROM approval_tokens")
        await db.execute("DELETE FROM proposals")
        await db.execute("DELETE FROM mandates")
        await db.execute("DELETE FROM user_sessions")
        await db.execute(
            """
            UPDATE system_controls
            SET kill_switch_active = 0, engaged_at = NULL, engaged_by = NULL, reason = NULL
            WHERE control_id = 1
            """
        )
        await db.commit()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield an async HTTP client wired to the FastAPI app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
