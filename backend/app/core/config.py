"""Application configuration loaded from environment variables and .env files."""

import json
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

from app.core.security import validate_signing_secret

# Directory paths for deterministic env file discovery
BACKEND_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Global application settings.

    Configuration Precedence (highest to lowest):
    1. Process environment variables (os.environ).
    2. Backend-specific .env file (backend/.env) if present.
    3. Workspace root .env file (.env) if present.
    4. Default values defined in this class.
    """

    APP_NAME: str = "LEASH"
    APP_VERSION: str = "0.2.0-dev"
    DEBUG: bool = False
    DATABASE_URL: str = "sqlite:///./leash.db"
    CORS_ORIGINS: list[str] | str = ["http://localhost:5173"]
    API_V1_PREFIX: str = "/api/v1"

    # Mandate signing key for HMAC-SHA256 (Phase 2A).
    # Loaded only from the environment or .env — never a source-code default.
    MANDATE_SIGNING_KEY: str = ""
    MANDATE_KEY_ALGORITHM: str = "HMAC-SHA256"

    # Administrative API key for emergency kill switch and audit inspection (Phase 2D).
    # Loaded strictly from environment or .env — never a source-code default.
    ADMIN_API_KEY: str = ""

    @field_validator("CORS_ORIGINS", mode="after")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        """Normalize and validate CORS_ORIGINS from JSON array or comma-separated string."""
        if isinstance(v, str):
            val = v.strip()
            if not val:
                raise ValueError("CORS_ORIGINS cannot be empty")
            if val.startswith("["):
                try:
                    parsed = json.loads(val)
                except Exception as exc:
                    raise ValueError(f"Invalid JSON array in CORS_ORIGINS: {exc}") from exc
                if not isinstance(parsed, list):
                    raise ValueError("CORS_ORIGINS JSON must be a list")
                origins = [str(item).strip() for item in parsed if str(item).strip()]
            else:
                origins = [item.strip() for item in val.split(",") if item.strip()]

            if not origins:
                raise ValueError("CORS_ORIGINS must contain at least one non-empty origin")
            return origins

        if isinstance(v, list):
            origins = [str(item).strip() for item in v if str(item).strip()]
            if not origins:
                raise ValueError("CORS_ORIGINS must contain at least one non-empty origin")
            return origins

        raise ValueError("Invalid type for CORS_ORIGINS")

    @field_validator("MANDATE_SIGNING_KEY", mode="after")
    @classmethod
    def validate_mandate_signing_key(cls, v: str) -> str:
        """Reject missing or weak mandate signing secrets at configuration load."""
        validate_signing_secret(v)
        return v

    @model_validator(mode="after")
    def validate_production_admin_api_key(self) -> "Settings":
        """Enforce minimum 32-character ADMIN_API_KEY when DEBUG=False."""
        if not self.DEBUG:
            key = self.ADMIN_API_KEY
            if not key or not isinstance(key, str) or len(key.encode("utf-8")) < 32:
                raise ValueError(
                    "ADMIN_API_KEY must be configured with at least 32 characters "
                    "when DEBUG=False (production mode)."
                )
        return self

    model_config = {
        "env_file": (
            str(ROOT_DIR / ".env"),
            str(BACKEND_DIR / ".env"),
        ),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
