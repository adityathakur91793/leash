"""FastAPI application entry-point for the LEASH backend."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.endpoints import router as v1_router
from app.core.config import settings
from app.db.database import init_db

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


class ShoppingAgentBodyLimitMiddleware:
    """Bound raw Shopping Agent request bodies, including chunked requests."""

    MAX_BODY_BYTES = 8192

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path", "").endswith("/shopping-agent/chat")
        ):
            await self.app(scope, receive, send)
            return

        messages = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                size += len(body)
                if size > self.MAX_BODY_BYTES:
                    await send({
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b'{"detail":"Shopping Agent request body is too large."}',
                    })
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break

        index = 0

        async def replay_receive() -> dict:
            nonlocal index
            if index < len(messages):
                current = messages[index]
                index += 1
                return current
            return await receive()

        await self.app(scope, replay_receive, send)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown lifecycle events."""
    logger.info("Starting %s v%s", settings.APP_NAME, settings.APP_VERSION)
    await init_db()
    yield
    logger.info("Shutting down %s", settings.APP_NAME)


app = FastAPI(
    title="LEASH API",
    description="The Trust Layer for Agentic Commerce",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ShoppingAgentBodyLimitMiddleware)


@app.get("/health")
async def health() -> dict[str, str]:
    """Lightweight liveness probe."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


app.include_router(v1_router, prefix=settings.API_V1_PREFIX)
