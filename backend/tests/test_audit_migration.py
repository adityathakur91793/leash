"""Tests for Phase 2D.1: Migration 5, Audit Schemas, and System Controls."""

from datetime import datetime, timezone
import json
import pytest

import aiosqlite

from app.core.config import settings
from app.db.database import (
    get_audit_checkpoints,
    get_db_path,
    get_kill_switch_status,
    get_latest_audit_entry,
    insert_audit_checkpoint,
    insert_audit_log_entry,
    is_kill_switch_active,
    list_audit_log_entries,
    set_kill_switch_state,
)
from app.schemas.audit import (
    AuditCheckpoint,
    AuditLogEntry,
    canonical_audit_hash,
    sign_checkpoint,
)
from app.schemas.kill_switch import (
    KillSwitchDisengageRequest,
    KillSwitchEngageRequest,
    KillSwitchStatus,
)

pytestmark = pytest.mark.asyncio


# --- Migration 5 Schema Tests ---

async def test_migration_5_schema_version_and_tables() -> None:
    """Verify Migration 5 creates audit_logs, audit_checkpoints, and system_controls."""
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 5

        for table_name in ("audit_logs", "audit_checkpoints", "system_controls"):
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
            table_row = await cursor.fetchone()
            assert table_row is not None, f"Table '{table_name}' must exist."


async def test_genesis_audit_record_and_checkpoint_exist() -> None:
    """Verify sequence 1 genesis record and genesis checkpoint exist with valid hashes."""
    latest = await get_latest_audit_entry()
    assert latest is not None
    assert latest.sequence_number >= 1

    entries, total = await list_audit_log_entries(limit=10, offset=0)
    assert total >= 1
    genesis = entries[0]
    assert genesis.sequence_number == 1
    assert genesis.event_type == "GENESIS"
    assert genesis.entity_type == "SYSTEM"
    assert genesis.actor == "SYSTEM"
    assert genesis.prev_hash == "0" * 64

    # Verify canonical hash integrity of genesis entry
    recomputed = genesis.compute_canonical_hash()
    assert genesis.entry_hash == recomputed

    # Verify genesis checkpoint
    checkpoints = await get_audit_checkpoints()
    assert len(checkpoints) >= 1
    genesis_cp = checkpoints[0]
    assert genesis_cp.sequence_number == 1
    assert genesis_cp.entry_hash == genesis.entry_hash

    # Verify checkpoint HMAC signature — no fallback key permitted.
    # settings.MANDATE_SIGNING_KEY is always valid here because Settings()
    # enforces it at startup; if it were empty the application would not start.
    expected_sig = sign_checkpoint(
        1, genesis.entry_hash, genesis_cp.sealed_at, settings.MANDATE_SIGNING_KEY
    )
    assert genesis_cp.checkpoint_sig == expected_sig


# --- System Controls & Kill Switch Tests ---

async def test_kill_switch_initial_state_disengaged() -> None:
    """Verify emergency kill switch initializes to disengaged (active=False)."""
    active = await is_kill_switch_active()
    assert active is False

    status = await get_kill_switch_status()
    assert status.kill_switch_active is False
    assert status.engaged_at is None
    assert status.engaged_by is None
    assert status.reason is None


async def test_kill_switch_state_transitions() -> None:
    """Verify engaging and disengaging the emergency kill switch updates persistent state."""
    # 1. Engage kill switch
    now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
    status = await set_kill_switch_state(
        active=True,
        engaged_by="admin_sec_team",
        reason="Anomalous proposal volume detected.",
        now=now,
    )
    assert status.kill_switch_active is True
    assert status.engaged_by == "admin_sec_team"
    assert status.reason == "Anomalous proposal volume detected."
    assert status.engaged_at == now

    assert await is_kill_switch_active() is True

    # 2. Disengage kill switch
    disengage_now = datetime(2026, 11, 5, 12, 30, 0, tzinfo=timezone.utc)
    status_dis = await set_kill_switch_state(
        active=False,
        engaged_by=None,
        reason="Security audit cleared.",
        now=disengage_now,
    )
    assert status_dis.kill_switch_active is False
    assert status_dis.engaged_at is None
    assert status_dis.engaged_by is None
    assert status_dis.reason == "Security audit cleared."

    assert await is_kill_switch_active() is False


# --- Schema & Canonical Hashing Tests ---

async def test_canonical_audit_hash_rejects_floats() -> None:
    """Verify canonical audit hashing strictly forbids floating-point values."""
    envelope_with_float = {
        "sequence_number": 2,
        "timestamp": "2026-11-05T12:00:00+00:00",
        "event_type": "PROPOSAL_CREATED",
        "entity_type": "PROPOSAL",
        "entity_id": "prp_1",
        "actor": "AGENT",
        "prev_hash": "a" * 64,
        "payload": {
            "total_float": 19.99,  # FLOAT FORBIDDEN
        },
    }
    with pytest.raises(ValueError, match="Floating-point numbers are strictly forbidden"):
        canonical_audit_hash(envelope_with_float)


async def test_canonical_audit_hash_key_ordering_invariance() -> None:
    """Verify key ordering does not alter the canonical hash."""
    env1 = {
        "sequence_number": 2,
        "timestamp": "2026-11-05T12:00:00+00:00",
        "event_type": "PROPOSAL_CREATED",
        "entity_type": "PROPOSAL",
        "entity_id": "prp_1",
        "actor": "AGENT",
        "prev_hash": "0" * 64,
        "payload": {"a": "first", "b": "second"},
    }
    env2 = {
        "payload": {"b": "second", "a": "first"},
        "actor": "AGENT",
        "entity_id": "prp_1",
        "entity_type": "PROPOSAL",
        "event_type": "PROPOSAL_CREATED",
        "timestamp": "2026-11-05T12:00:00+00:00",
        "sequence_number": 2,
        "prev_hash": "0" * 64,
    }
    hash1 = canonical_audit_hash(env1)
    hash2 = canonical_audit_hash(env2)
    assert hash1 == hash2


async def test_audit_checkpoint_signature_generation() -> None:
    """Verify sign_checkpoint produces deterministic HMAC-SHA256 signature."""
    now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
    sig1 = sign_checkpoint(10, "f" * 64, now, "test-secret-key-12345")
    sig2 = sign_checkpoint(10, "f" * 64, now, "test-secret-key-12345")
    assert sig1 == sig2
    assert len(sig1) == 64


async def test_kill_switch_request_validation() -> None:
    """Verify Pydantic models for kill-switch requests enforce bounds."""
    req = KillSwitchEngageRequest(reason="Valid lockdown reason", engaged_by="admin")
    assert req.reason == "Valid lockdown reason"
    assert req.engaged_by == "admin"

    with pytest.raises(ValueError):
        # reason too short
        KillSwitchEngageRequest(reason="no", engaged_by="admin")


# --- Database Helpers Integration Test ---

async def test_audit_helpers_insertion_and_listing() -> None:
    """Verify insert_audit_log_entry and list_audit_log_entries."""
    now = datetime(2026, 11, 5, 12, 10, 0, tzinfo=timezone.utc)
    prev = await get_latest_audit_entry()
    assert prev is not None

    seq = prev.sequence_number + 1
    envelope = {
        "sequence_number": seq,
        "timestamp": now.isoformat(),
        "event_type": "MANDATE_CREATED",
        "entity_type": "MANDATE",
        "entity_id": "man_test_123",
        "actor": "USER",
        "payload": {"budget": "50.00"},
        "prev_hash": prev.entry_hash,
    }
    e_hash = canonical_audit_hash(envelope)

    entry = AuditLogEntry(
        sequence_number=seq,
        timestamp=now,
        event_type="MANDATE_CREATED",
        entity_type="MANDATE",
        entity_id="man_test_123",
        actor="USER",
        payload={"budget": "50.00"},
        prev_hash=prev.entry_hash,
        entry_hash=e_hash,
    )
    await insert_audit_log_entry(entry)

    # Verify latest entry updated
    new_latest = await get_latest_audit_entry()
    assert new_latest is not None
    assert new_latest.sequence_number == seq
    assert new_latest.entry_hash == e_hash

    # Verify filtered listing
    mandates_entries, total = await list_audit_log_entries(entity_type="MANDATE")
    assert total >= 1
    assert any(m.entity_id == "man_test_123" for m in mandates_entries)

    # Insert checkpoint
    sig = sign_checkpoint(seq, e_hash, now, settings.MANDATE_SIGNING_KEY)
    cp = AuditCheckpoint(
        sequence_number=seq,
        entry_hash=e_hash,
        sealed_at=now,
        sealed_by="TEST_RUNNER",
        checkpoint_sig=sig,
    )
    await insert_audit_checkpoint(cp)

    cps = await get_audit_checkpoints()
    assert any(c.sequence_number == seq for c in cps)


# ---------------------------------------------------------------------------
# Security Correction Tests — added per Phase 2D.1 security review
# ---------------------------------------------------------------------------

# --- PF-1: No fallback key permitted ---

async def test_genesis_checkpoint_signed_with_mandate_key() -> None:
    """PF-1: Genesis checkpoint must be signed with MANDATE_SIGNING_KEY, not a fallback.

    Verifies that the stored genesis checkpoint signature matches a recompute
    using settings.MANDATE_SIGNING_KEY directly (no 'or fallback' expression).
    """
    checkpoints = await get_audit_checkpoints()
    assert len(checkpoints) >= 1
    genesis_cp = checkpoints[0]
    assert genesis_cp.sequence_number == 1

    entries, _ = await list_audit_log_entries(limit=1, offset=0)
    assert len(entries) >= 1
    genesis_entry = entries[0]

    # Recompute using MANDATE_SIGNING_KEY directly — no fallback.
    expected = sign_checkpoint(
        1, genesis_entry.entry_hash, genesis_cp.sealed_at, settings.MANDATE_SIGNING_KEY
    )
    assert genesis_cp.checkpoint_sig == expected, (
        "Genesis checkpoint signature does not match MANDATE_SIGNING_KEY. "
        "A fallback key may have been used — this is not permitted."
    )


async def test_fallback_key_does_not_match_genesis_signature() -> None:
    """PF-1: Demonstrate that the static fallback key produces a different signature.

    If genesis was signed with 'genesis_fallback_key', the MANDATE_SIGNING_KEY
    verification above would fail — this test makes that relationship explicit.
    """
    checkpoints = await get_audit_checkpoints()
    genesis_cp = checkpoints[0]
    entries, _ = await list_audit_log_entries(limit=1, offset=0)
    genesis_entry = entries[0]

    fallback_sig = sign_checkpoint(
        1, genesis_entry.entry_hash, genesis_cp.sealed_at, "genesis_fallback_key"
    )
    # The stored signature must NOT equal the fallback-key signature
    # (unless MANDATE_SIGNING_KEY happens to be "genesis_fallback_key", which
    # would fail the 32-byte minimum length check at startup).
    assert genesis_cp.checkpoint_sig != fallback_sig, (
        "Genesis checkpoint was signed with the disallowed static fallback key!"
    )


# --- PF-3: Duplicate checkpoint sequence rejection ---

async def test_duplicate_checkpoint_sequence_rejected() -> None:
    """PF-3: audit_checkpoints.sequence_number must have a UNIQUE constraint.

    Attempting to insert two checkpoints for the same sequence number must
    raise an IntegrityError.
    """
    now = datetime(2026, 11, 5, 14, 0, 0, tzinfo=timezone.utc)
    # Get the genesis entry for a valid entry_hash
    entries, _ = await list_audit_log_entries(limit=1, offset=0)
    genesis = entries[0]

    sig = sign_checkpoint(
        99999, genesis.entry_hash, now, settings.MANDATE_SIGNING_KEY
    )
    cp1 = AuditCheckpoint(
        sequence_number=99999,
        entry_hash=genesis.entry_hash,
        sealed_at=now,
        sealed_by="TEST_RUNNER",
        checkpoint_sig=sig,
    )
    await insert_audit_checkpoint(cp1)

    # Inserting a second checkpoint for the same sequence_number must fail.
    cp2 = AuditCheckpoint(
        sequence_number=99999,
        entry_hash=genesis.entry_hash,
        sealed_at=now,
        sealed_by="TEST_RUNNER_2",
        checkpoint_sig=sig,
    )
    with pytest.raises(Exception):  # aiosqlite wraps as IntegrityError / OperationalError
        await insert_audit_checkpoint(cp2)


# --- NF-1: Decimal rejection in canonical hash ---

async def test_canonical_audit_hash_rejects_decimal() -> None:
    """NF-1: Decimal values must be explicitly rejected with a clear ValueError."""
    from decimal import Decimal

    envelope_with_decimal = {
        "sequence_number": 2,
        "timestamp": "2026-11-05T12:00:00+00:00",
        "event_type": "PROPOSAL_CREATED",
        "entity_type": "PROPOSAL",
        "entity_id": "prp_decimal_test",
        "actor": "AGENT",
        "prev_hash": "a" * 64,
        "payload": {"total": Decimal("19.99")},
    }
    with pytest.raises(ValueError, match="Decimal values are not permitted"):
        canonical_audit_hash(envelope_with_decimal)


async def test_canonical_audit_hash_rejects_nested_decimal() -> None:
    """NF-1: Decimal rejection must be recursive — nested Decimals are also caught."""
    from decimal import Decimal

    envelope = {
        "sequence_number": 3,
        "timestamp": "2026-11-05T12:00:00+00:00",
        "event_type": "MANDATE_CREATED",
        "entity_type": "MANDATE",
        "entity_id": "man_decimal_test",
        "actor": "USER",
        "prev_hash": "b" * 64,
        "payload": {
            "line_items": [
                {"price": Decimal("5.00")},  # nested inside list
            ]
        },
    }
    with pytest.raises(ValueError, match="Decimal values are not permitted"):
        canonical_audit_hash(envelope)


async def test_canonical_audit_hash_accepts_fixed_decimal_strings() -> None:
    """NF-1: Monetary values encoded as fixed two-decimal strings are valid.

    This confirms the documented pattern: convert Decimal to str before hashing.
    """
    envelope = {
        "sequence_number": 4,
        "timestamp": "2026-11-05T12:00:00+00:00",
        "event_type": "PROPOSAL_CREATED",
        "entity_type": "PROPOSAL",
        "entity_id": "prp_string_decimal",
        "actor": "AGENT",
        "prev_hash": "c" * 64,
        "payload": {"total": "19.99", "shipping": "0.00"},  # strings — OK
    }
    result = canonical_audit_hash(envelope)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


# --- NF-2: Entry hash self-verification ---

async def test_insert_audit_log_entry_accepts_valid_hash() -> None:
    """NF-2: insert_audit_log_entry must succeed when entry_hash is correct."""
    now = datetime(2026, 11, 5, 15, 0, 0, tzinfo=timezone.utc)
    prev = await get_latest_audit_entry()
    assert prev is not None

    seq = prev.sequence_number + 1
    envelope = {
        "sequence_number": seq,
        "timestamp": now.isoformat(),
        "event_type": "MANDATE_CREATED",
        "entity_type": "MANDATE",
        "entity_id": "man_hash_valid",
        "actor": "USER",
        "payload": {"budget": "75.00"},
        "prev_hash": prev.entry_hash,
    }
    correct_hash = canonical_audit_hash(envelope)

    entry = AuditLogEntry(
        sequence_number=seq,
        timestamp=now,
        event_type="MANDATE_CREATED",
        entity_type="MANDATE",
        entity_id="man_hash_valid",
        actor="USER",
        payload={"budget": "75.00"},
        prev_hash=prev.entry_hash,
        entry_hash=correct_hash,
    )
    # Must not raise
    await insert_audit_log_entry(entry)

    latest = await get_latest_audit_entry()
    assert latest is not None
    assert latest.sequence_number == seq
    assert latest.entry_hash == correct_hash


async def test_insert_audit_log_entry_rejects_tampered_entry_hash() -> None:
    """NF-2: insert_audit_log_entry must reject a mismatched entry_hash with ValueError."""
    now = datetime(2026, 11, 5, 15, 30, 0, tzinfo=timezone.utc)
    prev = await get_latest_audit_entry()
    assert prev is not None

    seq = prev.sequence_number + 1
    entry_with_fake_hash = AuditLogEntry(
        sequence_number=seq,
        timestamp=now,
        event_type="MANDATE_CREATED",
        entity_type="MANDATE",
        entity_id="man_tampered",
        actor="USER",
        payload={"budget": "50.00"},
        prev_hash=prev.entry_hash,
        entry_hash="d" * 64,  # deliberate fake hash — will not match recomputed value
    )
    with pytest.raises(ValueError, match="entry_hash mismatch"):
        await insert_audit_log_entry(entry_with_fake_hash)


async def test_insert_audit_log_entry_rejects_tampered_payload() -> None:
    """NF-2: Modifying payload after hash computation must be detected.

    The entry_hash is computed over the original payload. If the payload is
    changed before insertion, the recomputed hash will differ and the insert
    must be rejected.
    """
    now = datetime(2026, 11, 5, 15, 45, 0, tzinfo=timezone.utc)
    prev = await get_latest_audit_entry()
    assert prev is not None

    seq = prev.sequence_number + 1
    # Compute hash over original payload
    original_payload = {"budget": "100.00"}
    envelope = {
        "sequence_number": seq,
        "timestamp": now.isoformat(),
        "event_type": "MANDATE_CREATED",
        "entity_type": "MANDATE",
        "entity_id": "man_payload_tampered",
        "actor": "USER",
        "payload": original_payload,
        "prev_hash": prev.entry_hash,
    }
    correct_hash = canonical_audit_hash(envelope)

    # Now construct entry with DIFFERENT payload but same (now-wrong) hash
    entry_tampered = AuditLogEntry(
        sequence_number=seq,
        timestamp=now,
        event_type="MANDATE_CREATED",
        entity_type="MANDATE",
        entity_id="man_payload_tampered",
        actor="USER",
        payload={"budget": "999.99"},  # tampered: budget changed after hashing
        prev_hash=prev.entry_hash,
        entry_hash=correct_hash,     # hash is now stale — does not match tampered payload
    )
    with pytest.raises(ValueError, match="entry_hash mismatch"):
        await insert_audit_log_entry(entry_tampered)
