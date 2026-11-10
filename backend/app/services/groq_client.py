"""Small server-side Groq REST client using existing httpx dependency."""

import json
import re
from typing import Any

import httpx

from app.core.config import settings

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_GROQ_RESPONSE_BYTES = 256_000


class GroqConfigurationError(Exception):
    """Raised when the backend-only Groq configuration is unavailable."""


class GroqProviderError(Exception):
    """Raised for safe, non-specific Groq transport or response failures."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        stage: str = "initial_generation",
        category: str = "provider_error",
        provider_http_status: int | None = None,
        provider_code: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.stage = stage
        self.category = category
        self.provider_http_status = provider_http_status
        self.provider_code = provider_code


class GroqRateLimitError(GroqProviderError):
    """Raised when Groq returns a rate-limit response."""


class GroqClient:
    """Async chat completions client using Groq's OpenAI-compatible API."""

    provider = "groq"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        raw_key = api_key if api_key is not None else settings.GROQ_API_KEY
        self._api_key = raw_key.strip()
        self._model = model or settings.GROQ_MODEL
        self._client = client

    @property
    def model(self) -> str:
        """Configured Groq model identifier."""
        return self._model

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        request_id: str | None = None,
        stage: str = "initial_generation",
    ) -> dict[str, Any]:
        """Send chat messages and return OpenAI-compatible JSON envelope."""
        if not self._api_key:
            raise GroqConfigurationError("Groq API key is not configured.")

        body = {
            "model": self._model,
            "messages": messages,
            "tools": GROQ_FUNCTION_DECLARATIONS,
            "tool_choice": "auto",
            "max_tokens": 512,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                async with self._client.stream(
                    "POST",
                    GROQ_API_URL,
                    headers=headers,
                    json=body,
                    timeout=20.0,
                ) as response:
                    return await self._read_response(
                        response, request_id=request_id, stage=stage
                    )
            async with httpx.AsyncClient(timeout=20.0) as client:
                async with client.stream(
                    "POST",
                    GROQ_API_URL,
                    headers=headers,
                    json=body,
                ) as response:
                    return await self._read_response(
                        response, request_id=request_id, stage=stage
                    )
        except (GroqRateLimitError, GroqProviderError):
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise GroqProviderError(
                "Shopping Agent provider is temporarily unavailable.",
                request_id=request_id,
                stage=stage,
                category="network_error",
            ) from exc

    @staticmethod
    async def _read_response(
        response: httpx.Response,
        *,
        request_id: str | None = None,
        stage: str = "initial_generation",
    ) -> dict[str, Any]:
        """Read and validate bounded streaming response bytes."""
        if response.status_code < 200 or response.status_code >= 300:
            provider_category, provider_code = await _safe_groq_error_metadata(response)
            error_type = (
                GroqRateLimitError
                if response.status_code == 429
                else GroqProviderError
            )
            message = (
                "Shopping Agent is rate limited. Try again later."
                if response.status_code == 429
                else "Shopping Agent provider returned an error."
            )
            raise error_type(
                message,
                request_id=request_id,
                stage=stage,
                category=provider_category,
                provider_http_status=response.status_code,
                provider_code=provider_code,
            )
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_GROQ_RESPONSE_BYTES:
                raise GroqProviderError(
                    "Shopping Agent provider response exceeded its limit.",
                    request_id=request_id,
                    stage=stage,
                    category="response_too_large",
                    provider_http_status=response.status_code,
                )
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GroqProviderError(
                "Shopping Agent provider response was malformed.",
                request_id=request_id,
                stage=stage,
                category="malformed_response",
                provider_http_status=response.status_code,
            ) from exc
        if not isinstance(parsed, dict):
            raise GroqProviderError(
                "Shopping Agent provider response was malformed.",
                request_id=request_id,
                stage=stage,
                category="malformed_response",
                provider_http_status=response.status_code,
            )
        return parsed


async def _safe_groq_error_metadata(
    response: httpx.Response,
) -> tuple[str, int | str | None]:
    """Extract only a bounded, allowlisted category and code from provider errors."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > 16_384:
            return "provider_error", None
    try:
        envelope = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "provider_error", None
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if not isinstance(error, dict):
        return "provider_error", None

    # OpenAI-compatible error shapes contain "type" and/or "code"
    raw_category = error.get("type") or error.get("code")
    category = (
        raw_category
        if isinstance(raw_category, str)
        and re.fullmatch(r"[A-Za-z0-9_]{1,64}", raw_category)
        else "provider_error"
    )
    raw_code = error.get("code")
    if isinstance(raw_code, int) and 100 <= raw_code <= 599:
        provider_code: int | str | None = raw_code
    elif isinstance(raw_code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", raw_code):
        provider_code = raw_code
    else:
        provider_code = None
    return category, provider_code


GROQ_FUNCTION_DECLARATIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search the local simulated product catalogue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Optional name or description keyword.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional category filter.",
                    },
                    "merchant": {
                        "type": "string",
                        "description": "Optional canonical merchant ID.",
                    },
                    "min_price": {
                        "type": "number",
                        "description": "Optional inclusive minimum price.",
                    },
                    "max_price": {
                        "type": "number",
                        "description": "Optional inclusive maximum price.",
                    },
                    "currency": {
                        "type": "string",
                        "description": "Required when using price filters.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Result limit from 1 to 25.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": "Look up one exact stable ID from the local simulated catalogue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "Exact stable catalogue ID.",
                    }
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_purchase",
            "description": (
                "Submit a product proposal for deterministic LEASH policy evaluation. "
                "This does not approve or pay for a purchase."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "Exact catalogue ID returned by a tool.",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Requested quantity, 1 to 10000.",
                    },
                },
                "required": ["product_id", "quantity"],
            },
        },
    },
]
