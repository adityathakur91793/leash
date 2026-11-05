"""Comprehensive test suite for Phase 2D.2: Hash Chain, Checkpoints, and Verification."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json

import aiosqlite
import pytest

from app.core.config import settings
from app.db.database import (
    get_all_audit_entries,
    get_audit_checkpoints,
    get_audit_entry_by_sequence,
    get_checkpoint_by_sequence,
    get_db_path,
    get_latest_audit_checkpoint,
    get_latest_audit_entry,
    insert_audit_checkpoint,
    insert_audit_log_entry,
)
from app.schemas.audit import (
    AuditActor,
    AuditCheckpoint,
    AuditEntityType,
    AuditEventType,
    AuditLogEntry,
    AuditVerifyResponse,
    canonical_audit_hash,
    sign_checkpoint,
)
from app.services.audit_service import AuditService

pytestmark = pytest.mark.asyncio


# --- 1. Hash-Linked Audit Append Tests ---

async def test_append_event_success_and_chaining() -> None:
    """Verify append_event links sequentially to preceding entry hash."""
    entry1 = await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CREATED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_test_001",
        actor=AuditActor.USER,
        payload={"budget": "100.00"},
        auto_checkpoint=False,
    )
    assert entry1.sequence_number == 2
    genesis = await get_audit_entry_by_sequence(1)
    assert genesis is not None
    assert entry1.prev_hash == genesis.entry_hash
    assert entry1.entry_hash == entry1.compute_canonical_hash()

    entry2 = await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CONFIRMED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_test_001",
        actor=AuditActor.USER,
        payload={"confirmed": True},
        auto_checkpoint=False,
    )
    assert entry2.sequence_number == 3
    assert entry2.prev_hash == entry1.entry_hash
    assert entry2.entry_hash == entry2.compute_canonical_hash()


async def test_append_event_rejects_floats() -> None:
    """Verify append_event raises ValueError if payload contains float."""
    with pytest.raises(ValueError, match="Floating-point numbers are strictly forbidden"):
        await AuditService.append_event(
            event_type=AuditEventType.PROPOSAL_CREATED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id="prp_001",
            actor=AuditActor.AGENT,
            payload={"price_float": 19.99},
        )


async def test_append_event_rejects_decimals() -> None:
    """Verify append_event raises ValueError if payload contains Decimal."""
    with pytest.raises(ValueError, match="Decimal values are not permitted"):
        await AuditService.append_event(
            event_type=AuditEventType.PROPOSAL_CREATED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id="prp_002",
            actor=AuditActor.AGENT,
            payload={"price_decimal": Decimal("19.99")},
        )


async def test_insert_audit_log_entry_rejects_non_contiguous_sequence() -> None:
    """Verify direct insert of non-contiguous sequence is rejected."""
    latest = await get_latest_audit_entry()
    assert latest is not None
    now = datetime.now(timezone.utc)

    # Gap: skip latest.sequence_number + 1, try + 2
    gap_seq = latest.sequence_number + 2
    envelope = {
        "actor": "USER",
        "entity_id": "man_gap",
        "entity_type": "MANDATE",
        "event_type": "MANDATE_CREATED",
        "payload": {"budget": "50.00"},
        "prev_hash": latest.entry_hash,
        "sequence_number": gap_seq,
        "timestamp": now.isoformat(),
    }
    e_hash = canonical_audit_hash(envelope)
    entry = AuditLogEntry(
        sequence_number=gap_seq,
        timestamp=now,
        event_type="MANDATE_CREATED",
        entity_type="MANDATE",
        entity_id="man_gap",
        actor="USER",
        payload={"budget": "50.00"},
        prev_hash=latest.entry_hash,
        entry_hash=e_hash,
    )
    with pytest.raises(ValueError, match="Non-contiguous sequence_number"):
        await insert_audit_log_entry(entry)


async def test_insert_audit_log_entry_rejects_invalid_prev_hash() -> None:
    """Verify direct insert with wrong prev_hash is rejected."""
    latest = await get_latest_audit_entry()
    assert latest is not None
    now = datetime.now(timezone.utc)
    next_seq = latest.sequence_number + 1
    wrong_prev = "e" * 64

    envelope = {
        "actor": "USER",
        "entity_id": "man_bad_prev",
        "entity_type": "MANDATE",
        "event_type": "MANDATE_CREATED",
        "payload": {"budget": "50.00"},
        "prev_hash": wrong_prev,
        "sequence_number": next_seq,
        "timestamp": now.isoformat(),
    }
    e_hash = canonical_audit_hash(envelope)
    entry = AuditLogEntry(
        sequence_number=next_seq,
        timestamp=now,
        event_type="MANDATE_CREATED",
        entity_type="MANDATE",
        entity_id="man_bad_prev",
        actor="USER",
        payload={"budget": "50.00"},
        prev_hash=wrong_prev,
        entry_hash=e_hash,
    )
    with pytest.raises(ValueError, match="Invalid prev_hash"):
        await insert_audit_log_entry(entry)


async def test_concurrent_appends_are_contiguous_and_unbroken() -> None:
    """Verify concurrent appends allocate contiguous sequences with unbroken chain."""
    num_concurrent = 15

    async def _append(idx: int) -> AuditLogEntry:
        return await AuditService.append_event(
            event_type=AuditEventType.PROPOSAL_CREATED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=f"prp_concurrent_{idx}",
            actor=AuditActor.AGENT,
            payload={"index": str(idx)},
            auto_checkpoint=False,
        )

    # Run tasks concurrently
    entries = await asyncio.gather(*[_append(i) for i in range(num_concurrent)])
    assert len(entries) == num_concurrent

    # Verify chain integrity
    res = await AuditService.verify_chain()
    assert res.valid is True
    assert res.total_records == 1 + num_concurrent  # genesis + 15
    assert res.corrupted_sequence is None


# --- 2. Checkpoint Creation Tests ---

async def test_create_checkpoint_for_tip_and_sequence() -> None:
    """Verify manual checkpoint creation and duplicate rejection."""
    # Append an event
    entry = await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CREATED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_cp_test",
        actor=AuditActor.USER,
        payload={"budget": "250.00"},
        auto_checkpoint=False,
    )
    assert entry.sequence_number == 2

    # Create checkpoint for sequence 2
    cp = await AuditService.create_checkpoint(sequence_number=2, sealed_by="ADMIN")
    assert cp.sequence_number == 2
    assert cp.entry_hash == entry.entry_hash
    assert cp.sealed_by == "ADMIN"

    # Verify signature
    expected_sig = sign_checkpoint(
        2, entry.entry_hash, cp.sealed_at, settings.MANDATE_SIGNING_KEY
    )
    assert cp.checkpoint_sig == expected_sig

    # Attempting to create duplicate checkpoint for sequence 2 must fail
    with pytest.raises(ValueError, match="already exists"):
        await AuditService.create_checkpoint(sequence_number=2)


async def test_create_checkpoint_rejects_nonexistent_sequence() -> None:
    """Verify checkpoint creation for non-existent audit sequence is rejected."""
    with pytest.raises(ValueError, match="does not exist"):
        await AuditService.create_checkpoint(sequence_number=99999)


async def test_auto_checkpoint_at_cadence() -> None:
    """Verify automatic checkpoint creation when sequence % cadence == 0."""
    cadence = 5
    # Append events up to sequence 5
    # genesis is sequence 1, so append 4 events to reach sequence 5
    for i in range(4):
        await AuditService.append_event(
            event_type=AuditEventType.PROPOSAL_CREATED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=f"prp_auto_cp_{i}",
            actor=AuditActor.AGENT,
            payload={"item": str(i)},
            auto_checkpoint=True,
            cadence=cadence,
        )

    # Checkpoint at sequence 5 must exist
    cp5 = await get_checkpoint_by_sequence(5)
    assert cp5 is not None
    assert cp5.sequence_number == 5
    assert cp5.sealed_by == "SYSTEM_AUTO"


# --- 3. Cryptographic Chain Verification Tests ---

async def test_verify_chain_on_initial_state() -> None:
    """Verify fresh database with genesis passes verification."""
    res = await AuditService.verify_chain()
    assert res.valid is True
    assert res.total_records == 1
    assert res.total_checkpoints == 1
    assert res.verified_sequence_range == (1, 1)
    assert res.corrupted_sequence is None
    assert res.error_message is None
    assert res.anomalies == []


async def test_verify_chain_clean_multirecord_with_cadence() -> None:
    """Verify a clean multi-record chain with cadence checkpoints."""
    cadence = 3
    # Append up to sequence 6 (cadence points at 3 and 6)
    for i in range(5):
        await AuditService.append_event(
            event_type=AuditEventType.PROPOSAL_CREATED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=f"prp_clean_{i}",
            actor=AuditActor.AGENT,
            payload={"amount": "10.00"},
            auto_checkpoint=True,
            cadence=cadence,
        )

    res = await AuditService.verify_chain(cadence=cadence)
    assert res.valid is True
    assert res.total_records == 6
    assert res.total_checkpoints == 3  # seq 1, seq 3, seq 6
    assert res.latest_checkpoint_sequence == 6
    assert res.verified_sequence_range == (1, 6)
    assert res.anomalies == []


async def test_verify_chain_detects_tampered_entry_payload() -> None:
    """Verify that tampering with an audit log payload in the database is detected."""
    await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CREATED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_tamper",
        actor=AuditActor.USER,
        payload={"budget": "100.00"},
        auto_checkpoint=False,
    )

    # Simulate attacker tampering directly with payload_json in SQLite
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE audit_logs SET payload_json = ? WHERE sequence_number = 2",
            (json.dumps({"budget": "999999.00"}, sort_keys=True, separators=(",", ":")),),
        )
        await db.commit()

    res = await AuditService.verify_chain()
    assert res.valid is False
    assert res.corrupted_sequence == 2
    assert "CORRUPTED_ENTRY_HASH" in res.anomalies
    assert "Canonical entry hash mismatch" in (res.error_message or "")


async def test_verify_chain_detects_broken_hash_link() -> None:
    """Verify that an altered prev_hash is detected as BROKEN_HASH_LINK."""
    await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CREATED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_link_1",
        actor=AuditActor.USER,
        payload={"budget": "100.00"},
        auto_checkpoint=False,
    )
    await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CONFIRMED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_link_1",
        actor=AuditActor.USER,
        payload={"confirmed": True},
        auto_checkpoint=False,
    )

    # Tamper with prev_hash and re-sign entry 3 so canonical hash matches,
    # but the prev_hash link to entry 2 is severed.
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        fake_prev = "9" * 64
        env = {
            "actor": "USER",
            "entity_id": "man_link_1",
            "entity_type": "MANDATE",
            "event_type": "MANDATE_CONFIRMED",
            "payload": {"confirmed": True},
            "prev_hash": fake_prev,
            "sequence_number": 3,
            "timestamp": "2026-11-05T12:00:00+00:00",
        }
        tampered_hash = canonical_audit_hash(env)
        await db.execute(
            """
            UPDATE audit_logs
            SET prev_hash = ?, entry_hash = ?, timestamp = ?
            WHERE sequence_number = 3
            """,
            (fake_prev, tampered_hash, "2026-11-05T12:00:00+00:00"),
        )
        await db.commit()

    res = await AuditService.verify_chain()
    assert res.valid is False
    assert res.corrupted_sequence == 3
    assert "BROKEN_HASH_LINK" in res.anomalies


async def test_verify_chain_detects_sequence_gap() -> None:
    """Verify deletion of an intermediate record is detected as SEQUENCE_GAP."""
    for i in range(3):
        await AuditService.append_event(
            event_type=AuditEventType.PROPOSAL_CREATED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=f"prp_gap_{i}",
            actor=AuditActor.AGENT,
            payload={"index": str(i)},
            auto_checkpoint=False,
        )

    # Delete intermediate record sequence 2
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM audit_logs WHERE sequence_number = 2")
        await db.commit()

    res = await AuditService.verify_chain()
    assert res.valid is False
    assert res.corrupted_sequence == 2
    assert "SEQUENCE_GAP" in res.anomalies


async def test_verify_chain_detects_invalid_checkpoint_signature() -> None:
    """Verify that a forged or tampered checkpoint signature is detected."""
    await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CREATED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_sig_tamper",
        actor=AuditActor.USER,
        payload={"budget": "100.00"},
        auto_checkpoint=False,
    )
    cp = await AuditService.create_checkpoint(sequence_number=2)

    # Tamper with checkpoint_sig in database
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE audit_checkpoints SET checkpoint_sig = ? WHERE sequence_number = 2",
            ("0" * 64,),
        )
        await db.commit()

    res = await AuditService.verify_chain()
    assert res.valid is False
    assert res.corrupted_sequence == 2
    assert "INVALID_CHECKPOINT_SIGNATURE" in res.anomalies


async def test_verify_chain_detects_missing_genesis_checkpoint() -> None:
    """Verify that deleting the genesis checkpoint is detected."""
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM audit_checkpoints WHERE sequence_number = 1")
        await db.commit()

    res = await AuditService.verify_chain()
    assert res.valid is False
    assert res.corrupted_sequence == 1
    assert "MISSING_GENESIS_CHECKPOINT" in res.anomalies


async def test_verify_chain_detects_tail_truncation_anomaly() -> None:
    """Verify that deleting audit records while leaving checkpoint triggers TAIL_TRUNCATION."""
    # Append events and create a checkpoint at sequence 3
    await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CREATED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_trunc_1",
        actor=AuditActor.USER,
        payload={"budget": "100.00"},
        auto_checkpoint=False,
    )
    await AuditService.append_event(
        event_type=AuditEventType.MANDATE_CONFIRMED,
        entity_type=AuditEntityType.MANDATE,
        entity_id="man_trunc_1",
        actor=AuditActor.USER,
        payload={"confirmed": True},
        auto_checkpoint=False,
    )
    await AuditService.create_checkpoint(sequence_number=3)

    # Attacker deletes record sequence 3 from audit_logs, leaving checkpoint at sequence 3
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM audit_logs WHERE sequence_number = 3")
        await db.commit()

    res = await AuditService.verify_chain()
    assert res.valid is False
    assert "TAIL_TRUNCATION_ANOMALY" in res.anomalies
    assert res.corrupted_sequence == 3  # tip was 2, checkpoint is 3 -> flag tip + 1


async def test_verify_chain_detects_stale_checkpoint_anomaly() -> None:
    """Verify that missing a scheduled cadence checkpoint triggers STALE_CHECKPOINT_ANOMALY."""
    cadence = 3
    # Append 5 records without auto_checkpoint (tip is sequence 6)
    # Checkpoints at seq 3 and seq 6 are expected by cadence=3
    for i in range(5):
        await AuditService.append_event(
            event_type=AuditEventType.PROPOSAL_CREATED,
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=f"prp_stale_{i}",
            actor=AuditActor.AGENT,
            payload={"index": str(i)},
            auto_checkpoint=False,  # deliberate omission of checkpoints
        )

    res = await AuditService.verify_chain(cadence=cadence)
    assert res.valid is False
    assert res.corrupted_sequence == 3
    assert "STALE_CHECKPOINT_ANOMALY" in res.anomalies
    assert "Missing scheduled checkpoint" in (res.error_message or "")
