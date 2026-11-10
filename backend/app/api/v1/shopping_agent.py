"""Authenticated Shopping Agent API."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_current_session
from app.db.database import list_mandates_for_owner_from_db
from app.schemas.mandate import MandateStatus
from app.schemas.session import UserSession
from app.schemas.shopping_agent import (
    OwnedMandateOption,
    ShoppingAgentRequest,
    ShoppingAgentResponse,
)
from app.services.gemini_client import (
    GeminiConfigurationError,
    GeminiProviderError,
    GeminiRateLimitError,
)
from app.services.groq_client import (
    GroqConfigurationError,
    GroqProviderError,
    GroqRateLimitError,
)
from app.services.mandate_service import (
    MandateNotFoundError,
    get_mandate,
    verify_mandate,
)
from app.services.shopping_agent_service import (
    ShoppingAgentBlockedError,
    ShoppingAgentInputError,
    ShoppingAgentOwnershipError,
    run_shopping_agent,
)
from app.services.intent_auditor_client import IntentAuditorUnavailable

router = APIRouter(prefix="/shopping-agent", tags=["Shopping Agent"])
logger = logging.getLogger(__name__)


def _log_provider_failure(
    exc: GeminiProviderError | GroqProviderError,
    provider: str = "AI",
) -> None:
    """Log bounded provider diagnostics without request, response, or credential data."""
    logger.warning(
        "Shopping Agent %s failure request_id=%s stage=%s category=%s "
        "provider_http_status=%s provider_code=%s",
        provider,
        getattr(exc, "request_id", None) or "unavailable",
        getattr(exc, "stage", "initial_generation"),
        getattr(exc, "category", "provider_error"),
        getattr(exc, "provider_http_status", None) or "unavailable",
        getattr(exc, "provider_code", None) or "unavailable",
    )


def _log_gemini_failure(exc: GeminiProviderError) -> None:
    _log_provider_failure(exc, provider="Gemini")


@router.get("/mandates", response_model=list[OwnedMandateOption])
async def list_owned_mandates(
    session: UserSession = Depends(get_current_session),
) -> list[OwnedMandateOption]:
    """List the authenticated principal's active, verified mandates."""
    options: list[OwnedMandateOption] = []
    for stored in await list_mandates_for_owner_from_db(session.user_id, limit=100):
        mandate = await get_mandate(stored.mandate_id)
        if mandate.owner_user_id != session.user_id:
            continue
        verification = await verify_mandate(mandate.mandate_id)
        if not verification.valid or mandate.status != MandateStatus.ACTIVE:
            continue
        options.append(
            OwnedMandateOption(
                mandate_id=mandate.mandate_id,
                currency=mandate.currency,
                max_total_budget=mandate.max_total_budget,
                expires_at=mandate.expires_at.isoformat(),
            )
        )
    return options


@router.post("/chat", response_model=ShoppingAgentResponse)
async def chat(
    payload: ShoppingAgentRequest,
    session: UserSession = Depends(get_current_session),
) -> ShoppingAgentResponse:
    """Chat through Gemini with only catalogue and proposal tools."""
    try:
        return await run_shopping_agent(
            payload.message,
            user_id=session.user_id,
            mandate_id=payload.mandate_id,
        )
    except ShoppingAgentOwnershipError as exc:
        raise HTTPException(
            status_code=403,
            detail="Selected mandate is not available to this session.",
        ) from exc
    except ShoppingAgentInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except MandateNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Selected mandate was not found.") from exc
    except (GeminiConfigurationError, GroqConfigurationError) as exc:
        raise HTTPException(status_code=503, detail="Shopping Agent is not configured.") from exc
    except (GeminiRateLimitError, GroqRateLimitError) as exc:
        _log_provider_failure(exc, provider="Groq" if isinstance(exc, GroqRateLimitError) else "Gemini")
        raise HTTPException(
            status_code=429,
            detail="Shopping Agent is busy. Try again shortly.",
        ) from exc
    except (GeminiProviderError, GroqProviderError) as exc:
        _log_provider_failure(exc, provider="Groq" if isinstance(exc, GroqProviderError) else "Gemini")
        raise HTTPException(
            status_code=502,
            detail="Shopping Agent provider is unavailable.",
        ) from exc
    except IntentAuditorUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Intent assessment is unavailable; proposal remains on hold.",
        ) from exc
    except ShoppingAgentBlockedError as exc:
        raise HTTPException(
            status_code=409,
            detail="Purchase proposal is blocked by the emergency control.",
        ) from exc
    except Exception as exc:
        # Avoid returning exception text that could include provider or DB details.
        raise HTTPException(
            status_code=500,
            detail="Shopping Agent request failed safely.",
        ) from exc
