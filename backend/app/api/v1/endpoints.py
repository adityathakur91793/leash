"""Version 1 API endpoints."""

from fastapi import APIRouter

from app.api.v1.activity import router as activity_router
from app.api.v1.admin import router as admin_router
from app.api.v1.approval import router as approval_router
from app.api.v1.mandates import router as mandates_router
from app.api.v1.payments import router as payments_router
from app.api.v1.proposals import router as proposals_router
from app.api.v1.sessions import router as sessions_router
from app.api.v1.shopping_agent import router as shopping_agent_router
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


router.include_router(sessions_router)
router.include_router(mandates_router)
router.include_router(proposals_router)
router.include_router(admin_router)
router.include_router(approval_router)
router.include_router(payments_router)
router.include_router(shopping_agent_router)
router.include_router(activity_router)
