"""Tests for proposal persistence, duplicates, and immutability."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiosqlite
import pytest
from pydantic import ValidationError

from app.db.database import get_db_path, get_proposal_from_db
from app.schemas.mandate import MandateCreate
from app.schemas.proposal import ProposalCreate, ProposalStatus
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.proposal_service import (
    CurrencyMismatchError,
    DuplicateProposalConflictError,
    ProposalImmutableError,
    ProposalTamperedError,
    create_proposal,
    get_proposal,
    list_proposals,
    reject_content_mutation,
    set_proposal_status,
)

pytestmark = pytest.mark.asyncio


def _mandate_payload() -> MandateCreate:
    return MandateCreate(
        max_total_budget=Decimal("40.00"),
        currency="USD",
        allowed_merchants=["amazon.com"],
        allowed_categories=["books"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )


def _proposal_payload(mandate_id: str, **overrides: object) -> ProposalCreate:
    data = {
        "mandate_id": mandate_id,
        "product_id": "sku-100",
        "product_name": "Hardcover Notebook",
        "merchant": "amazon.com",
        "category": "books",
        "quantity": 1,
        "unit_price": Decimal("12.50"),
        "currency": "USD",
        "tax": Decimal("1.00"),
        "shipping": Decimal("2.00"),
        "other_charges": Decimal("0.50"),
        "product_source": "https://amazon.com/dp/sku-100",
        "idempotency_key": "agent-req-notebook-1",
    }
    data.update(overrides)
    return ProposalCreate(**data)


async def test_valid_proposal_creation_and_retrieval() -> None:
    """Creating a proposal persists server-calculated totals."""
    mandate = await create_draft_mandate(_mandate_payload())
    proposal, created = await create_proposal(_proposal_payload(mandate.mandate_id))
    assert created is True
    assert proposal.proposal_id.startswith("prp_")
    assert proposal.mandate_id == mandate.mandate_id
    assert proposal.subtotal == Decimal("12.50")
    assert proposal.total == Decimal("16.00")
    assert proposal.status == ProposalStatus.SUBMITTED

    loaded = await get_proposal(proposal.proposal_id)
    assert loaded.total == Decimal("16.00")
    assert loaded.merchant == "amazon.com"


async def test_missing_mandate_rejected() -> None:
    """A proposal must reference an existing mandate."""
    from app.services.mandate_service import MandateNotFoundError

    with pytest.raises(MandateNotFoundError):
        await create_proposal(_proposal_payload("man_does_not_exist"))


async def test_currency_must_match_mandate() -> None:
    """Proposal currency must be consistent with the associated mandate."""
    mandate = await create_draft_mandate(_mandate_payload())
    with pytest.raises(CurrencyMismatchError):
        await create_proposal(_proposal_payload(mandate.mandate_id, currency="EUR"))


async def test_duplicate_fingerprint_returns_original() -> None:
    """Identical commercial terms reuse the original proposal."""
    mandate = await create_draft_mandate(_mandate_payload())
    first, created_first = await create_proposal(
        _proposal_payload(mandate.mandate_id, idempotency_key=None)
    )
    second, created_second = await create_proposal(
        _proposal_payload(mandate.mandate_id, idempotency_key=None)
    )
    assert created_first is True
    assert created_second is False
    assert second.proposal_id == first.proposal_id
    listed = await list_proposals(mandate_id=mandate.mandate_id)
    assert len(listed) == 1


async def test_idempotency_key_conflict_when_terms_differ() -> None:
    """Reusing an idempotency key with different terms is a conflict."""
    mandate = await create_draft_mandate(_mandate_payload())
    await create_proposal(_proposal_payload(mandate.mandate_id))
    with pytest.raises(DuplicateProposalConflictError):
        await create_proposal(
            _proposal_payload(
                mandate.mandate_id,
                product_id="sku-999",
                unit_price=Decimal("9.99"),
            )
        )


async def test_evaluated_proposal_is_immutable() -> None:
    """Evaluated/approved proposals cannot be modified in place."""
    mandate = await create_draft_mandate(_mandate_payload())
    proposal, _ = await create_proposal(_proposal_payload(mandate.mandate_id))
    evaluated = await set_proposal_status(proposal.proposal_id, ProposalStatus.EVALUATED)
    assert evaluated.status == ProposalStatus.EVALUATED

    with pytest.raises(ProposalImmutableError):
        await reject_content_mutation(proposal.proposal_id)

    approved = await set_proposal_status(proposal.proposal_id, ProposalStatus.APPROVED)
    assert approved.status == ProposalStatus.APPROVED
    assert approved.unit_price == Decimal("12.50")
    assert approved.total == Decimal("16.00")

    with pytest.raises(ProposalImmutableError):
        await set_proposal_status(proposal.proposal_id, ProposalStatus.SUBMITTED)


async def test_tampered_proposal_is_rejected() -> None:
    """Stored commercial-term tampering is detected via content hash."""
    mandate = await create_draft_mandate(_mandate_payload())
    proposal, _ = await create_proposal(_proposal_payload(mandate.mandate_id))

    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE proposals SET unit_price = ? WHERE proposal_id = ?",
            ("999.00", proposal.proposal_id),
        )
        await db.commit()

    with pytest.raises(ProposalTamperedError):
        await get_proposal(proposal.proposal_id)


async def test_new_proposal_required_for_changed_terms() -> None:
    """Changed terms create a new proposal instead of mutating the original."""
    mandate = await create_draft_mandate(_mandate_payload())
    original, _ = await create_proposal(
        _proposal_payload(mandate.mandate_id, idempotency_key="key-original")
    )
    await set_proposal_status(original.proposal_id, ProposalStatus.EVALUATED)
    replacement, created = await create_proposal(
        _proposal_payload(
            mandate.mandate_id,
            quantity=2,
            idempotency_key="key-replacement",
        )
    )
    assert created is True
    assert replacement.proposal_id != original.proposal_id
    assert replacement.subtotal == Decimal("25.00")
    unchanged = await get_proposal(original.proposal_id)
    assert unchanged.quantity == 1


async def test_mandate_lifecycle_unaffected_by_proposals() -> None:
    """Creating a proposal does not alter mandate signing or activation."""
    mandate = await create_draft_mandate(_mandate_payload())
    await create_proposal(_proposal_payload(mandate.mandate_id))
    confirmed = await confirm_mandate(mandate.mandate_id)
    signed = await sign_mandate(confirmed.mandate_id)
    active = await activate_mandate(signed.mandate_id)
    assert active.status.value == "ACTIVE"
    assert active.max_total_budget == Decimal("40.00")


async def test_insert_does_not_overwrite_existing_terms() -> None:
    """There is no in-place overwrite path for stored commercial terms."""
    mandate = await create_draft_mandate(_mandate_payload())
    proposal, _ = await create_proposal(_proposal_payload(mandate.mandate_id))
    loaded = await get_proposal_from_db(proposal.proposal_id)
    assert loaded is not None
    with pytest.raises((ProposalImmutableError, ValidationError, TypeError)):
        loaded.unit_price = Decimal("1.00")
        await reject_content_mutation(proposal.proposal_id)
