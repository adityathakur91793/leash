"""Application configuration loaded from environment variables and .env files."""

import json
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings

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
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    DATABASE_URL: str = "sqlite:///./leash.db"
    CORS_ORIGINS: list[str] | str = ["http://localhost:5173"]
    API_V1_PREFIX: str = "/api/v1"

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

    model_config = {
        "env_file": (
            str(ROOT_DIR / ".env"),
            str(BACKEND_DIR / ".env"),
        ),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
