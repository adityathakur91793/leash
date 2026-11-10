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

    # PayPal Sandbox Configuration (Phase 3).
    # Credentials loaded from environment or .env — never source-code defaults.
    PAYPAL_CLIENT_ID: str = ""
    PAYPAL_CLIENT_SECRET: str = ""
    PAYPAL_MODE: str = "sandbox"
    PAYPAL_SANDBOX_BASE_URL: str = "https://api-m.sandbox.paypal.com"

    # AI Provider configuration: "groq" (default) or "gemini".
    AI_PROVIDER: str = "groq"

    # Groq Shopping Agent configuration (backend-only).
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "qwen/qwen3.8-27b"

    # Gemini Shopping Agent configuration. The key remains backend-only.
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

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

    @field_validator("PAYPAL_MODE", mode="after")
    @classmethod
    def validate_paypal_mode(cls, v: str) -> str:
        """Enforce sandbox-only execution (₹0 budget constraint)."""
        mode = v.strip().lower()
        if mode != "sandbox":
            raise ValueError(
                f"Invalid PAYPAL_MODE '{v}'. Only 'sandbox' mode is permitted in LEASH."
            )
        return mode

    @field_validator("AI_PROVIDER", mode="after")
    @classmethod
    def validate_ai_provider(cls, value: str) -> str:
        """Allow only supported AI providers: 'groq' or 'gemini'."""
        provider = value.strip().lower()
        if provider not in {"groq", "gemini"}:
            raise ValueError(
                f"Invalid AI_PROVIDER '{value}'. Only 'groq' and 'gemini' are supported."
            )
        return provider

    @field_validator("GROQ_MODEL", mode="after")
    @classmethod
    def validate_groq_model(cls, value: str) -> str:
        """Keep the configurable Groq model identifier bounded and safe."""
        import re

        model = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", model):
            raise ValueError("GROQ_MODEL must be a valid model identifier.")
        return model

    @field_validator("GEMINI_MODEL", mode="after")
    @classmethod
    def validate_gemini_model(cls, value: str) -> str:
        """Keep the configurable model identifier out of URL syntax and paths."""
        import re

        model = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", model):
            raise ValueError("GEMINI_MODEL must be a simple model identifier.")
        return model

    @property
    def active_ai_model(self) -> str:
        """Return the active model name based on the configured AI provider."""
        if self.AI_PROVIDER == "groq":
            return self.GROQ_MODEL
        return self.GEMINI_MODEL

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
