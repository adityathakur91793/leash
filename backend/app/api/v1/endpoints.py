"""Version 1 API endpoints."""

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/status")
async def status() -> dict[str, str]:
    """Return the current operational status of the service."""
    return {
        "status": "operational",
        "version": settings.APP_VERSION,
        "service": settings.APP_NAME,
    }
