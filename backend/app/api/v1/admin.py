"""Admin API endpoints — kill switch control, audit log inspection.

All endpoints in this router require a valid X-Admin-Key header.
The admin key dependency uses constant-time comparison (hmac.compare_digest)
and never logs the key value.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.core.config import settings
from app.db.database import (
    get_audit_checkpoints,
    get_audit_entry_by_sequence,
    list_audit_log_entries,
)
from app.schemas.audit import (
    AuditCheckpoint,
    AuditLogEntry,
    AuditLogResponse,
    AuditVerifyResponse,
)
from app.schemas.kill_switch import (
    KillSwitchDisengageRequest,
    KillSwitchEngageRequest,
    KillSwitchStatus,
)
from app.services.audit_service import AuditService
from app.services.kill_switch_service import (
    disengage_kill_switch,
    engage_kill_switch,
    get_kill_switch_status_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

_MIN_KEY_LEN = 32


def require_admin(x_admin_key: str = Header(alias="X-Admin-Key")) -> None:
    """Validate X-Admin-Key header using constant-time comparison.

    Raises:
        503: If ADMIN_API_KEY is not configured or too short.
        403: If the provided key does not match.
    """
    configured_key = settings.ADMIN_API_KEY
    if not configured_key or len(configured_key) < _MIN_KEY_LEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured.",
        )
    if not hmac.compare_digest(configured_key, x_admin_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin API key.",
        )


# ---------------------------------------------------------------------------
# Kill switch endpoints
# ---------------------------------------------------------------------------


@router.get("/kill-switch", response_model=KillSwitchStatus)
async def get_kill_switch_endpoint(
    _: None = Depends(require_admin),
) -> KillSwitchStatus:
    """Return the current kill switch state."""
    try:
        return await get_kill_switch_status_service()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post("/kill-switch/engage", response_model=KillSwitchStatus)
async def engage_kill_switch_endpoint(
    body: KillSwitchEngageRequest,
    _: None = Depends(require_admin),
) -> KillSwitchStatus:
    """Engage the emergency kill switch."""
    try:
        return await engage_kill_switch(actor=body.engaged_by, reason=body.reason)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post("/kill-switch/disengage", response_model=KillSwitchStatus)
async def disengage_kill_switch_endpoint(
    body: KillSwitchDisengageRequest,
    _: None = Depends(require_admin),
) -> KillSwitchStatus:
    """Disengage the emergency kill switch."""
    try:
        return await disengage_kill_switch(actor=body.disengaged_by, reason=body.reason)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Audit log endpoints
# ---------------------------------------------------------------------------


@router.get("/audit/logs", response_model=AuditLogResponse)
async def list_audit_logs_endpoint(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    _: None = Depends(require_admin),
) -> AuditLogResponse:
    """List audit log entries with optional filtering and pagination."""
    items, total = await list_audit_log_entries(
        limit=limit,
        offset=offset,
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
    )
    return AuditLogResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/audit/logs/{sequence_number}", response_model=AuditLogEntry)
async def get_audit_log_entry_endpoint(
    sequence_number: int,
    _: None = Depends(require_admin),
) -> AuditLogEntry:
    """Retrieve a single audit log entry by sequence number."""
    entry = await get_audit_entry_by_sequence(sequence_number)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit log entry with sequence_number={sequence_number} not found.",
        )
    return entry


@router.get("/audit/verify", response_model=AuditVerifyResponse)
async def verify_audit_chain_endpoint(
    _: None = Depends(require_admin),
) -> AuditVerifyResponse:
    """Verify cryptographic integrity of the entire audit chain."""
    return await AuditService.verify_chain()


@router.get("/audit/checkpoints", response_model=list[AuditCheckpoint])
async def list_audit_checkpoints_endpoint(
    _: None = Depends(require_admin),
) -> list[AuditCheckpoint]:
    """List all audit chain checkpoints in ascending order."""
    return await get_audit_checkpoints()
