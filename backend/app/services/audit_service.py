"""Audit logging, checkpointing, and cryptographic chain verification service.

Note on Security Model:
    The SQLite append-only hash chain and HMAC-SHA256 checkpoints provide
    tamper-evidence, not tamper-proofing. An attacker with unrestricted physical
    or root access to the SQLite database file could coordinatedly truncate both
    audit records and checkpoints. Detecting such coordinated truncation requires
    an independently stored external anchor (e.g. distributed ledger, remote syslog,
    or external timestamping authority). Within the single SQLite database boundary,
    this service detects uncoordinated deletion, record tampering, sequence gaps,
    link corruption, orphan checkpoints, and stale checkpoints.
"""

from datetime import datetime, timezone
import logging
from typing import Any

import aiosqlite

from app.core.config import settings
from app.db.database import (
    get_all_audit_entries,
    get_audit_checkpoints,
    get_audit_entry_by_sequence,
    get_checkpoint_by_sequence,
    get_connection,
    get_latest_audit_checkpoint,
    get_latest_audit_entry,
    insert_audit_checkpoint,
    insert_audit_log_entry,
)
from app.schemas.audit import (
    DEFAULT_CHECKPOINT_CADENCE,
    AuditActor,
    AuditCheckpoint,
    AuditEntityType,
    AuditEventType,
    AuditLogEntry,
    AuditVerifyResponse,
    _assert_no_floats,
    canonical_audit_hash,
    sign_checkpoint,
)

logger = logging.getLogger(__name__)


class AuditService:
    """Service governing audit-chain appends, HMAC checkpointing, and verification."""

    @staticmethod
    async def append_event(
        event_type: str | AuditEventType,
        entity_type: str | AuditEntityType,
        entity_id: str,
        actor: str | AuditActor,
        payload: dict[str, Any],
        timestamp: datetime | None = None,
        db: aiosqlite.Connection | None = None,
        auto_checkpoint: bool = True,
        cadence: int = DEFAULT_CHECKPOINT_CADENCE,
    ) -> AuditLogEntry:
        """Atomically append a validated event to the hash-chained audit log.

        Guarantees:
        - Recomputes canonical hash via Restricted Deterministic JSON Profile.
        - Rejects floating-point and Decimal numbers in payload.
        - Contiguous sequence allocation locked via SQLite BEGIN IMMEDIATE.
        - Automatically creates HMAC checkpoint at configured cadence boundaries.
        """
        # Validate payload against float / Decimal values
        _assert_no_floats(payload)

        # Convert enums to str values if necessary
        event_type_str = (
            event_type.value if isinstance(event_type, AuditEventType) else str(event_type)
        )
        entity_type_str = (
            entity_type.value if isinstance(entity_type, AuditEntityType) else str(entity_type)
        )
        actor_str = actor.value if isinstance(actor, AuditActor) else str(actor)

        # Normalize timestamp to timezone-aware UTC
        ts = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
        ts_iso = ts.isoformat()

        async def _execute_append(conn: aiosqlite.Connection) -> AuditLogEntry:
            latest = await get_latest_audit_entry(conn)
            if latest is None:
                seq = 1
                prev_hash = "0" * 64
            else:
                seq = latest.sequence_number + 1
                prev_hash = latest.entry_hash

            envelope = {
                "actor": actor_str,
                "entity_id": str(entity_id),
                "entity_type": entity_type_str,
                "event_type": event_type_str,
                "payload": payload,
                "prev_hash": prev_hash,
                "sequence_number": seq,
                "timestamp": ts_iso,
            }
            entry_hash = canonical_audit_hash(envelope)

            entry = AuditLogEntry(
                sequence_number=seq,
                timestamp=ts,
                event_type=event_type_str,
                entity_type=entity_type_str,
                entity_id=str(entity_id),
                actor=actor_str,
                payload=payload,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )

            await insert_audit_log_entry(entry, db=conn)

            # Auto-checkpointing at cadence boundaries (e.g. seq=50, 100, 150)
            if auto_checkpoint and cadence > 0 and (seq % cadence == 0):
                await AuditService.create_checkpoint(
                    sequence_number=seq,
                    sealed_by="SYSTEM_AUTO",
                    db=conn,
                )

            return entry

        if db is not None:
            return await _execute_append(db)

        async with get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                entry = await _execute_append(conn)
                await conn.commit()
                return entry
            except Exception:
                await conn.rollback()
                raise

    @staticmethod
    async def create_checkpoint(
        sequence_number: int | None = None,
        sealed_by: str = "SYSTEM",
        db: aiosqlite.Connection | None = None,
    ) -> AuditCheckpoint:
        """Create and persist an HMAC-SHA256 checkpoint anchor for an audit sequence.

        Requirements:
        - Target audit entry must exist in audit_logs.
        - Target sequence_number must not already have a checkpoint.
        - HMAC signature computed strictly with settings.MANDATE_SIGNING_KEY.
        """
        if not settings.MANDATE_SIGNING_KEY:
            raise RuntimeError(
                "MANDATE_SIGNING_KEY is required to sign audit checkpoints. "
                "Ensure the key is configured in the environment."
            )

        async def _execute_checkpoint(conn: aiosqlite.Connection) -> AuditCheckpoint:
            nonlocal sequence_number
            if sequence_number is None:
                latest = await get_latest_audit_entry(conn)
                if latest is None:
                    raise ValueError("Cannot create checkpoint: audit log is empty.")
                target_entry = latest
                sequence_number = latest.sequence_number
            else:
                target_entry = await get_audit_entry_by_sequence(sequence_number, conn)
                if target_entry is None:
                    raise ValueError(
                        f"Cannot create checkpoint: audit entry sequence {sequence_number} "
                        "does not exist."
                    )

            existing_cp = await get_checkpoint_by_sequence(sequence_number, conn)
            if existing_cp is not None:
                raise ValueError(
                    f"Checkpoint for sequence {sequence_number} already exists."
                )

            now = datetime.now(timezone.utc)
            sig = sign_checkpoint(
                sequence_number,
                target_entry.entry_hash,
                now,
                settings.MANDATE_SIGNING_KEY,
            )

            checkpoint = AuditCheckpoint(
                sequence_number=sequence_number,
                entry_hash=target_entry.entry_hash,
                sealed_at=now,
                sealed_by=sealed_by,
                checkpoint_sig=sig,
            )

            await insert_audit_checkpoint(checkpoint, db=conn)
            logger.info(
                "Created audit checkpoint for sequence %d sealed by %s",
                sequence_number,
                sealed_by,
            )
            return checkpoint

        if db is not None:
            return await _execute_checkpoint(db)

        async with get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cp = await _execute_checkpoint(conn)
                await conn.commit()
                return cp
            except Exception:
                await conn.rollback()
                raise

    @staticmethod
    async def verify_chain(
        db: aiosqlite.Connection | None = None,
        cadence: int = DEFAULT_CHECKPOINT_CADENCE,
    ) -> AuditVerifyResponse:
        """Perform comprehensive cryptographic verification of the audit hash chain.

        Checks:
        1. Genesis record exists with sequence 1 and prev_hash == "0" * 64.
        2. Sequence numbers are strictly contiguous without gaps.
        3. Canonical entry hash matches recompute for every record.
        4. prev_hash matches the preceding entry's hash for all records > 1.
        5. Checkpoints reference existing records and exact entry hashes.
        6. Checkpoint HMAC-SHA256 signatures are valid against MANDATE_SIGNING_KEY.
        7. Checkpoint sequences are strictly increasing.
        8. Genesis checkpoint (sequence 1) exists.
        9. Checkpoints beyond the current tip are flagged as TAIL_TRUNCATION_ANOMALY.
        10. Stale / missing scheduled checkpoints are flagged as STALE_CHECKPOINT_ANOMALY.
        """
        async def _execute_verify(conn: aiosqlite.Connection) -> AuditVerifyResponse:
            entries = await get_all_audit_entries(conn)
            checkpoints = await get_audit_checkpoints(conn)

            anomalies: list[str] = []
            total_records = len(entries)
            total_checkpoints = len(checkpoints)

            # Check 1: Genesis record exists
            if total_records == 0:
                anomalies.append("MISSING_GENESIS_RECORD")
                return AuditVerifyResponse(
                    valid=False,
                    total_records=0,
                    total_checkpoints=total_checkpoints,
                    corrupted_sequence=1,
                    error_message="Audit log is empty: genesis record missing.",
                    anomalies=anomalies,
                )

            genesis = entries[0]
            if genesis.sequence_number != 1:
                anomalies.append("INVALID_GENESIS_SEQUENCE")
                return AuditVerifyResponse(
                    valid=False,
                    total_records=total_records,
                    total_checkpoints=total_checkpoints,
                    corrupted_sequence=genesis.sequence_number,
                    error_message=(
                        f"First audit entry has sequence {genesis.sequence_number}, expected 1."
                    ),
                    anomalies=anomalies,
                )

            # Check 2: Genesis previous hash is 64 zeroes
            if genesis.prev_hash != "0" * 64:
                anomalies.append("INVALID_GENESIS_PREV_HASH")
                return AuditVerifyResponse(
                    valid=False,
                    total_records=total_records,
                    total_checkpoints=total_checkpoints,
                    corrupted_sequence=1,
                    error_message="Genesis record previous hash must be 64 zeroes.",
                    anomalies=anomalies,
                )

            # Check 3, 4, 5: Contiguity, Canonical Hash, prev_hash link
            for i, entry in enumerate(entries):
                expected_seq = i + 1
                if entry.sequence_number != expected_seq:
                    anomalies.append("SEQUENCE_GAP")
                    return AuditVerifyResponse(
                        valid=False,
                        total_records=total_records,
                        total_checkpoints=total_checkpoints,
                        corrupted_sequence=expected_seq,
                        error_message=(
                            f"Audit sequence gap: expected sequence {expected_seq}, "
                            f"found {entry.sequence_number}."
                        ),
                        anomalies=anomalies,
                    )

                computed_hash = entry.compute_canonical_hash()
                if entry.entry_hash != computed_hash:
                    anomalies.append("CORRUPTED_ENTRY_HASH")
                    return AuditVerifyResponse(
                        valid=False,
                        total_records=total_records,
                        total_checkpoints=total_checkpoints,
                        corrupted_sequence=entry.sequence_number,
                        error_message=(
                            f"Canonical entry hash mismatch at sequence {entry.sequence_number}: "
                            f"stored '{entry.entry_hash[:12]}...', "
                            f"computed '{computed_hash[:12]}...'."
                        ),
                        anomalies=anomalies,
                    )

                if i > 0:
                    prev_entry = entries[i - 1]
                    if entry.prev_hash != prev_entry.entry_hash:
                        anomalies.append("BROKEN_HASH_LINK")
                        return AuditVerifyResponse(
                            valid=False,
                            total_records=total_records,
                            total_checkpoints=total_checkpoints,
                            corrupted_sequence=entry.sequence_number,
                            error_message=(
                                f"Broken hash chain link at sequence {entry.sequence_number}: "
                                f"prev_hash '{entry.prev_hash[:12]}...' does not match "
                                f"preceding entry_hash '{prev_entry.entry_hash[:12]}...'."
                            ),
                            anomalies=anomalies,
                        )

            verified_range = (1, total_records)
            latest_entry_hash = entries[-1].entry_hash
            tip_seq = entries[-1].sequence_number

            entry_map = {e.sequence_number: e for e in entries}
            latest_verified_cp_seq: int | None = None

            # Check 8: Checkpoints strictly increasing
            for j, cp in enumerate(checkpoints):
                if j > 0:
                    prev_cp = checkpoints[j - 1]
                    if cp.sequence_number <= prev_cp.sequence_number:
                        anomalies.append("NON_INCREASING_CHECKPOINT")
                        return AuditVerifyResponse(
                            valid=False,
                            total_records=total_records,
                            total_checkpoints=total_checkpoints,
                            verified_sequence_range=verified_range,
                            latest_entry_hash=latest_entry_hash,
                            corrupted_sequence=cp.sequence_number,
                            error_message=(
                                f"Checkpoint sequences are not strictly increasing: "
                                f"sequence {cp.sequence_number} <= {prev_cp.sequence_number}."
                            ),
                            anomalies=anomalies,
                        )

                # Check 10: Checkpoint exceeds chain tip (tail truncation)
                if cp.sequence_number > tip_seq:
                    anomalies.append("TAIL_TRUNCATION_ANOMALY")
                    return AuditVerifyResponse(
                        valid=False,
                        total_records=total_records,
                        total_checkpoints=total_checkpoints,
                        verified_sequence_range=verified_range,
                        latest_entry_hash=latest_entry_hash,
                        corrupted_sequence=tip_seq + 1,
                        error_message=(
                            f"Checkpoint sequence {cp.sequence_number} exceeds current "
                            f"audit chain tip {tip_seq} (possible tail-truncation detected)."
                        ),
                        anomalies=anomalies,
                    )

                # Check 6: Checkpoint references existing entry with exact entry_hash
                target_entry = entry_map.get(cp.sequence_number)
                if target_entry is None:
                    anomalies.append("ORPHAN_CHECKPOINT")
                    return AuditVerifyResponse(
                        valid=False,
                        total_records=total_records,
                        total_checkpoints=total_checkpoints,
                        verified_sequence_range=verified_range,
                        latest_entry_hash=latest_entry_hash,
                        corrupted_sequence=cp.sequence_number,
                        error_message=(
                            f"Checkpoint at sequence {cp.sequence_number} does not correspond "
                            "to any record in audit_logs."
                        ),
                        anomalies=anomalies,
                    )

                if cp.entry_hash != target_entry.entry_hash:
                    anomalies.append("CHECKPOINT_HASH_MISMATCH")
                    return AuditVerifyResponse(
                        valid=False,
                        total_records=total_records,
                        total_checkpoints=total_checkpoints,
                        verified_sequence_range=verified_range,
                        latest_entry_hash=latest_entry_hash,
                        corrupted_sequence=cp.sequence_number,
                        error_message=(
                            f"Checkpoint entry_hash mismatch at sequence {cp.sequence_number}: "
                            f"checkpoint='{cp.entry_hash[:12]}...', "
                            f"audit_log='{target_entry.entry_hash[:12]}...'."
                        ),
                        anomalies=anomalies,
                    )

                # Check 7: HMAC signature is valid
                if not settings.MANDATE_SIGNING_KEY:
                    raise RuntimeError("MANDATE_SIGNING_KEY is required to verify checkpoints.")

                expected_sig = sign_checkpoint(
                    cp.sequence_number,
                    cp.entry_hash,
                    cp.sealed_at,
                    settings.MANDATE_SIGNING_KEY,
                )
                if cp.checkpoint_sig != expected_sig:
                    anomalies.append("INVALID_CHECKPOINT_SIGNATURE")
                    return AuditVerifyResponse(
                        valid=False,
                        total_records=total_records,
                        total_checkpoints=total_checkpoints,
                        verified_sequence_range=verified_range,
                        latest_entry_hash=latest_entry_hash,
                        corrupted_sequence=cp.sequence_number,
                        error_message=(
                            f"Invalid HMAC-SHA256 signature for checkpoint at sequence "
                            f"{cp.sequence_number}."
                        ),
                        anomalies=anomalies,
                    )

                latest_verified_cp_seq = cp.sequence_number

            # Check 11: Missing Genesis Checkpoint
            has_genesis_cp = any(cp.sequence_number == 1 for cp in checkpoints)
            if not has_genesis_cp:
                anomalies.append("MISSING_GENESIS_CHECKPOINT")
                return AuditVerifyResponse(
                    valid=False,
                    total_records=total_records,
                    total_checkpoints=total_checkpoints,
                    verified_sequence_range=verified_range,
                    latest_entry_hash=latest_entry_hash,
                    latest_checkpoint_sequence=latest_verified_cp_seq,
                    corrupted_sequence=1,
                    error_message="Genesis checkpoint (sequence 1) is missing.",
                    anomalies=anomalies,
                )

            # Check 9: Cadence checkpoint completeness
            if cadence > 0:
                cp_seqs = {cp.sequence_number for cp in checkpoints}
                for scheduled_seq in range(cadence, tip_seq + 1, cadence):
                    if scheduled_seq not in cp_seqs:
                        anomalies.append("STALE_CHECKPOINT_ANOMALY")
                        return AuditVerifyResponse(
                            valid=False,
                            total_records=total_records,
                            total_checkpoints=total_checkpoints,
                            verified_sequence_range=verified_range,
                            latest_entry_hash=latest_entry_hash,
                            latest_checkpoint_sequence=latest_verified_cp_seq,
                            corrupted_sequence=scheduled_seq,
                            error_message=(
                                f"Missing scheduled checkpoint at sequence {scheduled_seq} "
                                f"(cadence={cadence}, tip={tip_seq})."
                            ),
                            anomalies=anomalies,
                        )

            # All checks passed successfully
            return AuditVerifyResponse(
                valid=True,
                total_records=total_records,
                total_checkpoints=total_checkpoints,
                verified_sequence_range=verified_range,
                latest_entry_hash=latest_entry_hash,
                latest_checkpoint_sequence=latest_verified_cp_seq,
                corrupted_sequence=None,
                error_message=None,
                anomalies=[],
            )

        if db is not None:
            return await _execute_verify(db)

        async with get_connection() as conn:
            return await _execute_verify(conn)
