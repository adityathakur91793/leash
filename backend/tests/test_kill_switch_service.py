"""Tests for Kill Switch service: persistence, atomic audit events, idempotency, and guards."""

import asyncio

import pytest

from app.db.database import get_connection, list_audit_log_entries
from app.schemas.audit import AuditEventType
from app.services.kill_switch_service import (
    disengage_kill_switch,
    engage_kill_switch,
    enforce_kill_switch_guard,
    get_kill_switch_status_service,
)


@pytest.mark.asyncio
async def test_kill_switch_initial_state_inactive() -> None:
    """Kill switch begins in inactive state upon database initialization."""
    status = await get_kill_switch_status_service()
    assert status.kill_switch_active is False
    assert status.engaged_at is None
    assert status.engaged_by is None


@pytest.mark.asyncio
async def test_kill_switch_engage_and_disengage_atomic_audit() -> None:
    """Engage and disengage persist state and atomic audit events."""
    # 1. Engage
    status = await engage_kill_switch(
        actor="admin_user_1",
        reason="Security incident investigation on payment gateway.",
    )
    assert status.kill_switch_active is True
    assert status.engaged_by == "admin_user_1"
    assert status.engaged_at is not None

    # Verify audit event was atomically written
    entries, total = await list_audit_log_entries(event_type=AuditEventType.KILL_SWITCH_ENGAGED)
    assert total >= 1
    latest_engaged = entries[-1]
    assert latest_engaged.actor == "ADMIN"
    assert latest_engaged.payload["actor"] == "admin_user_1"
    assert "Security incident" in latest_engaged.payload["reason"]

    # 2. Disengage
    status_off = await disengage_kill_switch(
        actor="admin_user_2",
        reason="Incident resolved, resuming standard traffic.",
    )
    assert status_off.kill_switch_active is False

    entries_off, total_off = await list_audit_log_entries(
        event_type=AuditEventType.KILL_SWITCH_DISENGAGED
    )
    assert total_off >= 1
    latest_disengaged = entries_off[-1]
    assert latest_disengaged.payload["actor"] == "admin_user_2"


@pytest.mark.asyncio
async def test_kill_switch_idempotence() -> None:
    """Repeated engage or disengage is idempotent and does not create duplicate audit records."""
    # First engage
    await engage_kill_switch(actor="admin", reason="Lockdown reason 1")
    entries_1, count_1 = await list_audit_log_entries(
        event_type=AuditEventType.KILL_SWITCH_ENGAGED
    )

    # Second engage (no-op)
    status_2 = await engage_kill_switch(actor="admin", reason="Lockdown reason 2")
    assert status_2.kill_switch_active is True

    entries_2, count_2 = await list_audit_log_entries(
        event_type=AuditEventType.KILL_SWITCH_ENGAGED
    )
    assert count_2 == count_1  # No duplicate audit log generated for redundant call

    # Disengage once
    await disengage_kill_switch(actor="admin", reason="Unlock reason 1")
    _, count_off_1 = await list_audit_log_entries(
        event_type=AuditEventType.KILL_SWITCH_DISENGAGED
    )

    # Disengage twice (no-op)
    status_off_2 = await disengage_kill_switch(actor="admin", reason="Unlock reason 2")
    assert status_off_2.kill_switch_active is False
    _, count_off_2 = await list_audit_log_entries(
        event_type=AuditEventType.KILL_SWITCH_DISENGAGED
    )
    assert count_off_2 == count_off_1


@pytest.mark.asyncio
async def test_enforce_kill_switch_guard() -> None:
    """enforce_kill_switch_guard passes when inactive and raises RuntimeError when active."""
    # Inactive: passes silently
    await enforce_kill_switch_guard()

    # Engage: must raise
    await engage_kill_switch(actor="admin", reason="Emergency stop active")
    with pytest.raises(RuntimeError) as exc:
        await enforce_kill_switch_guard()
    assert "Emergency kill switch is engaged" in str(exc.value)

    # Disengage: passes again
    await disengage_kill_switch(actor="admin", reason="Clearing emergency stop")
    await enforce_kill_switch_guard()


@pytest.mark.asyncio
async def test_enforce_kill_switch_guard_fails_closed_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard fails closed with RuntimeError when kill switch state cannot be determined."""
    async def _failing_is_kill_switch_active(*args, **kwargs):
        raise ConnectionError("Simulated database unreachable")

    monkeypatch.setattr(
        "app.services.kill_switch_service.is_kill_switch_active",
        _failing_is_kill_switch_active,
    )

    with pytest.raises(RuntimeError) as exc:
        await enforce_kill_switch_guard()
    assert "Kill switch state unavailable; failing closed." in str(exc.value)


@pytest.mark.asyncio
async def test_kill_switch_concurrent_engage_disengage() -> None:
    """Concurrent engage requests serialize cleanly without corrupting state."""
    results = await asyncio.gather(
        engage_kill_switch("admin_a", "Concurrent lock A"),
        engage_kill_switch("admin_b", "Concurrent lock B"),
        engage_kill_switch("admin_c", "Concurrent lock C"),
        return_exceptions=True,
    )

    # All calls should complete without database locking errors
    for r in results:
        assert not isinstance(r, Exception), f"Unexpected exception: {r}"
        assert r.kill_switch_active is True

    final_status = await get_kill_switch_status_service()
    assert final_status.kill_switch_active is True
