"""Authenticated, owner-scoped activity and audit overview endpoint."""

from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_session
from app.schemas.activity import ActivityOverviewResponse
from app.schemas.session import UserSession
from app.services.activity_service import get_activity_overview

router = APIRouter(prefix="/activity", tags=["Activity"])


@router.get("/overview", response_model=ActivityOverviewResponse)
async def get_activity_overview_endpoint(
    session: UserSession = Depends(get_current_session),
) -> ActivityOverviewResponse:
    """Return safe activity projections for the authenticated principal only."""
    return await get_activity_overview(session.user_id)
