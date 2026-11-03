"""Tests for Settings configuration, CORS deserialization, and environment discovery."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, ROOT_DIR, Settings


def test_cors_valid_json_array() -> None:
    """Test that a valid JSON array string parses correctly."""
    s = Settings(CORS_ORIGINS='["http://localhost:5173", "http://localhost:3000"]')
    assert s.CORS_ORIGINS == ["http://localhost:5173", "http://localhost:3000"]


def test_cors_comma_separated_origins() -> None:
    """Test that comma-separated origins parse into a list."""
    s = Settings(CORS_ORIGINS="http://localhost:5173, http://localhost:3000")
    assert s.CORS_ORIGINS == ["http://localhost:5173", "http://localhost:3000"]


def test_cors_single_origin() -> None:
    """Test that a single origin string without commas parses into a single-element list."""
    s = Settings(CORS_ORIGINS="http://localhost:5173")
    assert s.CORS_ORIGINS == ["http://localhost:5173"]


def test_cors_default_value() -> None:
    """Test that default CORS configuration preserves localhost origin."""
    s = Settings()
    assert s.CORS_ORIGINS == ["http://localhost:5173"]


@pytest.mark.parametrize(
    "invalid_input",
    [
        "",
        "   ",
        "[]",
        ",,",
        "[invalid-json",
        "['single-quote-invalid-json']",
    ],
)
def test_cors_invalid_configuration(invalid_input: str) -> None:
    """Test that malformed or empty CORS configurations are rejected with ValidationError."""
    with pytest.raises(ValidationError):
        Settings(CORS_ORIGINS=invalid_input)


def test_env_file_precedence_and_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that root .env is discovered, backend/.env overrides root, and env vars take top precedence."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir) / "project"
        tmp_backend = tmp_root / "backend"
        tmp_root.mkdir()
        tmp_backend.mkdir()

        root_env = tmp_root / ".env"
        backend_env = tmp_backend / ".env"

        # 1. Base settings from root .env
        root_env.write_text("APP_NAME=FromRootEnv\nDEBUG=true\n", encoding="utf-8")

        class CustomSettings(Settings):
            model_config = {
                "env_file": (str(root_env), str(backend_env)),
                "extra": "ignore",
            }

        s1 = CustomSettings()
        assert s1.APP_NAME == "FromRootEnv"
        assert s1.DEBUG is True

        # 2. backend/.env overrides root .env
        backend_env.write_text("APP_NAME=FromBackendEnv\n", encoding="utf-8")
        s2 = CustomSettings()
        assert s2.APP_NAME == "FromBackendEnv"
        assert s2.DEBUG is True  # preserved from root_env

        # 3. System environment variable takes top precedence
        monkeypatch.setenv("APP_NAME", "FromSystemEnv")
        s3 = CustomSettings()
        assert s3.APP_NAME == "FromSystemEnv"
        assert s3.DEBUG is True
