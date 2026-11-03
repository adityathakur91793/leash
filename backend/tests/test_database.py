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
