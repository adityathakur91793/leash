"""Purchase proposal service — creation, retrieval, and integrity checks.

Proposals are not authorizations and never trigger payment execution.
"""

import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

from app.db.database import (
    StoredProposalTamperedError,
    get_proposal_by_fingerprint,
    get_proposal_by_idempotency_key,
    get_proposal_from_db,
    insert_proposal_to_db,
    list_proposals_from_db,
    update_proposal_status_in_db,
)
from app.schemas.proposal import (
    CONTENT_LOCKED_STATUSES,
    Proposal,
    ProposalCreate,
    ProposalStatus,
    build_proposal_from_create,
)
from app.services.mandate_service import get_mandate

logger = logging.getLogger(__name__)

ALLOWED_STATUS_TRANSITIONS: dict[ProposalStatus, set[ProposalStatus]] = {
    ProposalStatus.SUBMITTED: {ProposalStatus.EVALUATED, ProposalStatus.WITHDRAWN},
    ProposalStatus.EVALUATED: {ProposalStatus.APPROVED, ProposalStatus.REJECTED},
}


class ProposalNotFoundError(Exception):
    """Raised when a proposal cannot be found."""


class ProposalTamperedError(Exception):
    """Raised when stored proposal contents fail integrity verification."""


class ProposalImmutableError(Exception):
    """Raised when a caller attempts to modify locked proposal terms."""


class DuplicateProposalConflictError(Exception):
    """Raised when an idempotency key is reused with different terms."""


class CurrencyMismatchError(Exception):
    """Raised when proposal currency does not match the associated mandate."""


class InvalidProposalStatusTransitionError(Exception):
    """Raised when an illegal proposal status transition is attempted."""


async def create_proposal(payload: ProposalCreate) -> tuple[Proposal, bool]:
    """Create a proposal or return the existing duplicate.

    Returns:
        A tuple of (proposal, created) where created is True only for inserts.
    """
    mandate = await get_mandate(payload.mandate_id)
    if mandate.currency != payload.currency:
        raise CurrencyMismatchError(
            f"Proposal currency '{payload.currency}' does not match mandate "
            f"currency '{mandate.currency}'."
        )

    created_at = datetime.now(timezone.utc)
    proposal_id = f"prp_{uuid.uuid4().hex}"
    proposal = build_proposal_from_create(
        payload, proposal_id=proposal_id, created_at=created_at
    )

    if payload.idempotency_key:
        existing_key = await get_proposal_by_idempotency_key(payload.idempotency_key)
        if existing_key is not None:
            if existing_key.fingerprint != proposal.fingerprint:
                raise DuplicateProposalConflictError(
                    "idempotency_key was reused with different proposal terms. "
                    "Submit a new proposal to change an evaluated request."
                )
            logger.info(
                "Returning existing proposal %s for idempotency key replay",
                existing_key.proposal_id,
            )
            return existing_key, False

    existing_fp = await get_proposal_by_fingerprint(proposal.fingerprint)
    if existing_fp is not None:
        logger.info(
            "Returning existing proposal %s for duplicate fingerprint",
            existing_fp.proposal_id,
        )
        return existing_fp, False

    try:
        await insert_proposal_to_db(proposal)
    except aiosqlite.IntegrityError:
        raced = await get_proposal_by_fingerprint(proposal.fingerprint)
        if raced is not None:
            return raced, False
        if payload.idempotency_key:
            raced_key = await get_proposal_by_idempotency_key(payload.idempotency_key)
            if raced_key is not None:
                if raced_key.fingerprint != proposal.fingerprint:
                    raise DuplicateProposalConflictError(
                        "idempotency_key was reused with different proposal terms."
                    ) from None
                return raced_key, False
        raise

    logger.info(
        "Created proposal %s for mandate %s",
        proposal.proposal_id,
        proposal.mandate_id,
    )
    return proposal, True


async def get_proposal(proposal_id: str) -> Proposal:
    """Retrieve a proposal, rejecting tampered stored contents."""
    try:
        proposal = await get_proposal_from_db(proposal_id)
    except StoredProposalTamperedError as exc:
        raise ProposalTamperedError(str(exc)) from exc
    if proposal is None:
        raise ProposalNotFoundError(f"Proposal with ID '{proposal_id}' was not found.")
    return proposal


async def list_proposals(
    limit: int = 50, mandate_id: str | None = None
) -> list[Proposal]:
    """List recent proposals, optionally scoped to one mandate."""
    if mandate_id is not None:
        await get_mandate(mandate_id)
    try:
        return await list_proposals_from_db(limit=limit, mandate_id=mandate_id)
    except StoredProposalTamperedError as exc:
        raise ProposalTamperedError(str(exc)) from exc


async def set_proposal_status(
    proposal_id: str, new_status: ProposalStatus
) -> Proposal:
    """Transition proposal status without altering commercial terms.

    This records evaluation outcome only. It does not authorize payment
    execution and does not mutate locked proposal fields.
    """
    proposal = await get_proposal(proposal_id)
    allowed = ALLOWED_STATUS_TRANSITIONS.get(proposal.status, set())
    if new_status not in allowed:
        if proposal.status in CONTENT_LOCKED_STATUSES:
            raise ProposalImmutableError(
                f"Proposal '{proposal_id}' in '{proposal.status.value}' state "
                "cannot be modified. Submit a new proposal instead."
            )
        raise InvalidProposalStatusTransitionError(
            f"Cannot transition proposal from '{proposal.status.value}' "
            f"to '{new_status.value}'."
        )

    updated = await update_proposal_status_in_db(
        proposal_id, new_status, proposal.status
    )
    if not updated:
        raise InvalidProposalStatusTransitionError(
            f"Proposal '{proposal_id}' status changed concurrently."
        )
    return await get_proposal(proposal_id)


async def reject_content_mutation(proposal_id: str) -> None:
    """Refuse in-place modification of commercial proposal terms."""
    proposal = await get_proposal(proposal_id)
    raise ProposalImmutableError(
        f"Proposal '{proposal.proposal_id}' commercial terms are immutable. "
        "Create a new proposal to request different purchase terms."
    )
