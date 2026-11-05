"""Emergency kill switch service — engage, disengage, guard.

This module provides the service layer for the persistent emergency kill switch.
State transitions and audit log records are atomically persisted in a single
SQLite transaction under BEGIN IMMEDIATE, guaranteeing fail-closed integrity.
"""

import logging

from app.db.database import (
    get_connection,
    get_kill_switch_status,
    is_kill_switch_active,
    set_kill_switch_state,
)
from app.schemas.audit import AuditActor, AuditEntityType, AuditEventType
from app.schemas.kill_switch import KillSwitchStatus
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

# Validation constants
_ACTOR_MIN = 1
_ACTOR_MAX = 100
_REASON_MIN = 3
_REASON_MAX = 500


def _validate_actor(actor: str) -> str:
    """Strip and validate actor string length."""
    actor = actor.strip()
    if len(actor) < _ACTOR_MIN or len(actor) > _ACTOR_MAX:
        raise ValueError(
            f"actor must be between {_ACTOR_MIN} and {_ACTOR_MAX} characters."
        )
    return actor


def _validate_reason(reason: str) -> str:
    """Validate reason string length."""
    if len(reason) < _REASON_MIN or len(reason) > _REASON_MAX:
        raise ValueError(
            f"reason must be between {_REASON_MIN} and {_REASON_MAX} characters."
        )
    return reason


async def engage_kill_switch(actor: str, reason: str) -> KillSwitchStatus:
    """Engage the emergency kill switch idempotently and atomically with audit log.

    Args:
        actor: Identity of the engaging administrator (1–100 chars, stripped).
        reason: Detailed explanation for lockdown (3–500 chars).

    Returns:
        Current KillSwitchStatus after operation.
    """
    actor = _validate_actor(actor)
    reason = _validate_reason(reason)

    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            current = await get_kill_switch_status(db=db)
            if current.kill_switch_active:
                await db.rollback()
                return current

            await set_kill_switch_state(
                active=True,
                engaged_by=actor,
                reason=reason,
                db=db,
            )
            # Atomically persist audit event within the same SQLite transaction
            await AuditService.append_event(
                event_type=AuditEventType.KILL_SWITCH_ENGAGED,
                entity_type=AuditEntityType.SYSTEM,
                entity_id="system",
                actor=AuditActor.ADMIN,
                payload={"actor": actor, "reason": reason[:200]},
                db=db,
            )
            await db.commit()
            return await get_kill_switch_status(db=db)
        except Exception:
            await db.rollback()
            raise


async def disengage_kill_switch(actor: str, reason: str) -> KillSwitchStatus:
    """Disengage the emergency kill switch idempotently and atomically with audit log.

    Args:
        actor: Identity of the disengaging administrator (1–100 chars, stripped).
        reason: Detailed explanation for releasing lockdown (3–500 chars).

    Returns:
        Current KillSwitchStatus after operation.
    """
    actor = _validate_actor(actor)
    reason = _validate_reason(reason)

    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            current = await get_kill_switch_status(db=db)
            if not current.kill_switch_active:
                await db.rollback()
                return current

            await set_kill_switch_state(
                active=False,
                engaged_by=actor,
                reason=reason,
                db=db,
            )
            # Atomically persist audit event within the same SQLite transaction
            await AuditService.append_event(
                event_type=AuditEventType.KILL_SWITCH_DISENGAGED,
                entity_type=AuditEntityType.SYSTEM,
                entity_id="system",
                actor=AuditActor.ADMIN,
                payload={"actor": actor, "reason": reason[:200]},
                db=db,
            )
            await db.commit()
            return await get_kill_switch_status(db=db)
        except Exception:
            await db.rollback()
            raise


async def get_kill_switch_status_service() -> KillSwitchStatus:
    """Return current kill switch status.

    Returns:
        Current KillSwitchStatus.
    """
    return await get_kill_switch_status()


async def enforce_kill_switch_guard() -> None:
    """Raise RuntimeError if kill switch is active. Fail closed on DB errors.

    Raises:
        RuntimeError: If the kill switch is active or its state cannot be read.
    """
    try:
        active = await is_kill_switch_active()
    except Exception as exc:
        raise RuntimeError("Kill switch state unavailable; failing closed.") from exc
    if active:
        raise RuntimeError(
            "Emergency kill switch is engaged. Purchase authorization is blocked."
        )
