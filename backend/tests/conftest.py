"""Shared test fixtures for the LEASH backend test suite."""

import os
import tempfile
from collections.abc import AsyncIterator

import pytest
import httpx

# Point the database at a temporary file BEFORE importing the app so that
# the Settings singleton picks up the override.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

from app.main import app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield an async HTTP client wired to the FastAPI app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
