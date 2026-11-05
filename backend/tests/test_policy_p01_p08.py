"""Unit tests for Policy Engine Rules P01 through P08 (Phase 2C.2)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.core.security import sign_payload
from app.schemas.mandate import Mandate, MandateStatus
from app.schemas.policy import EvaluationContext, RuleStatus
from app.schemas.proposal import Proposal, ProposalStatus
from app.services.policy_engine import (
    canonicalize_merchant_domain,
    evaluate_p01_signature,
    evaluate_p02_expiration,
    evaluate_p03_budget,
    evaluate_p04_item_price,
    evaluate_p05_merchant,
    evaluate_p06_category,
    evaluate_p07_quantity,
    evaluate_p08_currency,
)


@pytest.fixture
def base_now() -> datetime:
    return datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def base_context(base_now: datetime) -> EvaluationContext:
    return EvaluationContext(
        now=base_now,
        today_reserved=Decimal("0.00"),
        daily_txn_limit=Decimal("100.00"),
        cumulative_mandate_spent=Decimal("0.00"),
    )


@pytest.fixture
def base_mandate(base_now: datetime) -> Mandate:
    mandate_dict = {
        "mandate_id": "man_test_123",
        "schema_version": "1.0",
        "status": MandateStatus.ACTIVE,
        "max_total_budget": Decimal("40.00"),
        "currency": "USD",
        "allowed_merchants": ["amazon.com", "xn--bcher-kva.de"],
        "allowed_categories": ["books", "office-supplies"],
        "max_item_count": 5,
        "max_quantity_per_item": 2,
        "max_price_per_item": Decimal("25.00"),
        "allow_subscriptions": False,
        "require_user_approval": False,
        "created_at": base_now - timedelta(days=1),
        "expires_at": base_now + timedelta(days=1),
        "signed_at": base_now - timedelta(hours=1),
        "signature_algorithm": "HMAC-SHA256",
    }
    m = Mandate(**mandate_dict)
    # Compute signature with current MANDATE_SIGNING_KEY
    sig = sign_payload(m.canonical_payload(), settings.MANDATE_SIGNING_KEY)
    m.signature = sig
    return m


@pytest.fixture
def base_proposal(base_now: datetime) -> Proposal:
    return Proposal(
        proposal_id="prp_test_123",
        mandate_id="man_test_123",
        schema_version="1.0",
        status=ProposalStatus.SUBMITTED,
        product_id="sku-100",
        product_name="Hardcover Notebook",
        merchant="amazon.com",
        category="books",
        quantity=1,
        unit_price=Decimal("12.50"),
        currency="USD",
        subtotal=Decimal("12.50"),
        tax=Decimal("1.00"),
        shipping=Decimal("2.00"),
        other_charges=Decimal("0.50"),
        total=Decimal("16.00"),
        product_source="https://amazon.com/dp/sku-100",
        created_at=base_now,
        fingerprint="a" * 64,
        content_hash="b" * 64,
    )


# --- Merchant Domain Normalization Tests ---
def test_canonicalize_merchant_domain_valid() -> None:
    norm, err = canonicalize_merchant_domain("Amazon.COM")
    assert err is None
    assert norm == "amazon.com"


def test_canonicalize_merchant_domain_idna() -> None:
    norm, err = canonicalize_merchant_domain("Bücher.DE")
    assert err is None
    assert norm == "xn--bcher-kva.de"


@pytest.mark.parametrize(
    "raw_m",
    [
        "https://amazon.com",
        "amazon.com/path",
        "user:pass@amazon.com",
        "amazon.com:443",
        "amazon .com",
        "",
    ],
)
def test_canonicalize_merchant_domain_invalid(raw_m: str) -> None:
    norm, err = canonicalize_merchant_domain(raw_m)
    assert norm is None
    assert err is not None


# --- P01 Signature Tests ---
def test_p01_signature_valid(base_mandate: Mandate, base_context: EvaluationContext) -> None:
    res = evaluate_p01_signature(base_mandate, base_context)
    assert res.status == RuleStatus.PASS


def test_p01_signature_missing(base_mandate: Mandate, base_context: EvaluationContext) -> None:
    base_mandate.signature = None
    res = evaluate_p01_signature(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p01_signature_mismatch(base_mandate: Mandate, base_context: EvaluationContext) -> None:
    base_mandate.signature = "a" * 64
    res = evaluate_p01_signature(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p01_signed_at_in_future(base_mandate: Mandate, base_context: EvaluationContext) -> None:
    base_mandate.signed_at = base_context.now + timedelta(seconds=10)
    # Re-sign with future timestamp
    base_mandate.signature = sign_payload(
        base_mandate.canonical_payload(), settings.MANDATE_SIGNING_KEY
    )
    res = evaluate_p01_signature(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p01_unsupported_signature_algorithm(
    base_mandate: Mandate, base_context: EvaluationContext
) -> None:
    base_mandate.signature_algorithm = "RSA-SHA256"
    res = evaluate_p01_signature(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK
    assert "Unsupported signature algorithm" in res.reason


def test_p01_empty_signing_key(
    base_mandate: Mandate, base_context: EvaluationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MANDATE_SIGNING_KEY", "")
    res = evaluate_p01_signature(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK
    assert "Mandate signing key is empty" in res.reason


# --- P02 Expiration Tests ---
def test_p02_expiration_valid(base_mandate: Mandate, base_context: EvaluationContext) -> None:
    res = evaluate_p02_expiration(base_mandate, base_context)
    assert res.status == RuleStatus.PASS


def test_p02_mandate_revoked(base_mandate: Mandate, base_context: EvaluationContext) -> None:
    base_mandate.status = MandateStatus.REVOKED
    res = evaluate_p02_expiration(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p02_mandate_expired_boundary(
    base_mandate: Mandate, base_context: EvaluationContext
) -> None:
    base_mandate.expires_at = base_context.now
    res = evaluate_p02_expiration(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p02_mandate_draft_status(
    base_mandate: Mandate, base_context: EvaluationContext
) -> None:
    base_mandate.status = MandateStatus.DRAFT
    res = evaluate_p02_expiration(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK
    assert "expected 'ACTIVE'" in res.reason


def test_p02_mandate_pending_signature_status(
    base_mandate: Mandate, base_context: EvaluationContext
) -> None:
    base_mandate.status = MandateStatus.SIGNED
    res = evaluate_p02_expiration(base_mandate, base_context)
    assert res.status == RuleStatus.BLOCK
    assert "expected 'ACTIVE'" in res.reason


# --- P03 Budget Tests ---
def test_p03_budget_within(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    res = evaluate_p03_budget(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.PASS


def test_p03_budget_single_proposal_exceeded(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.max_total_budget = Decimal("15.00")
    res = evaluate_p03_budget(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p03_budget_cumulative_exceeded(
    base_mandate: Mandate, base_proposal: Proposal, base_now: datetime
) -> None:
    ctx = EvaluationContext(
        now=base_now,
        cumulative_mandate_spent=Decimal("30.00"),  # 30 + 16 = 46 > 40
    )
    res = evaluate_p03_budget(base_mandate, base_proposal, ctx)
    assert res.status == RuleStatus.BLOCK


def test_p03_budget_cannot_be_overridden_by_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.require_user_approval = True
    base_mandate.max_total_budget = Decimal("10.00")  # Proposal is 16.00
    res = evaluate_p03_budget(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK  # Never REQUIRE_APPROVAL!


# --- P04 Per-Item Spending Limit Tests ---
def test_p04_item_price_within(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    res = evaluate_p04_item_price(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.PASS


def test_p04_item_price_exceeded_no_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.max_price_per_item = Decimal("10.00")
    base_mandate.require_user_approval = False
    res = evaluate_p04_item_price(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p04_item_price_exceeded_with_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.max_price_per_item = Decimal("10.00")
    base_mandate.require_user_approval = True
    res = evaluate_p04_item_price(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.REQUIRE_APPROVAL


def test_p04_item_price_not_applicable(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.max_price_per_item = None
    res = evaluate_p04_item_price(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.NOT_APPLICABLE


# --- P05 Merchant Allowlist Tests ---
def test_p05_merchant_exact_match(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    res = evaluate_p05_merchant(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.PASS


def test_p05_merchant_idna_match(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_proposal.merchant = "bücher.de"
    res = evaluate_p05_merchant(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.PASS


def test_p05_merchant_unlisted_no_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_proposal.merchant = "target.com"
    base_mandate.require_user_approval = False
    res = evaluate_p05_merchant(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p05_merchant_unlisted_with_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_proposal.merchant = "target.com"
    base_mandate.require_user_approval = True
    res = evaluate_p05_merchant(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.REQUIRE_APPROVAL


def test_p05_merchant_empty_allowlist(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.allowed_merchants = []
    res = evaluate_p05_merchant(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p05_merchant_subdomain_prohibited(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_proposal.merchant = "store.amazon.com"
    base_mandate.require_user_approval = False
    res = evaluate_p05_merchant(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK


# --- P06 Category Allowlist Tests ---
def test_p06_category_allowed(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    res = evaluate_p06_category(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.PASS


def test_p06_category_case_insensitive(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    # Mandate allows "books"
    base_proposal.category = "  Books  "
    res = evaluate_p06_category(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.PASS
    assert res.evidence["category"] == "books"


def test_p06_category_empty_allowlist_blocks_all(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.allowed_categories = []
    res = evaluate_p06_category(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p06_category_unlisted_with_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_proposal.category = "electronics"
    base_mandate.require_user_approval = True
    res = evaluate_p06_category(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.REQUIRE_APPROVAL


# --- P07 Quantity Tests ---
def test_p07_quantity_within_limits(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    res = evaluate_p07_quantity(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.PASS


def test_p07_quantity_exceeds_per_item_no_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_proposal.quantity = 3  # limit is 2
    base_mandate.require_user_approval = False
    res = evaluate_p07_quantity(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK


def test_p07_quantity_exceeds_per_item_with_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_proposal.quantity = 3
    base_mandate.require_user_approval = True
    res = evaluate_p07_quantity(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.REQUIRE_APPROVAL


def test_p07_quantity_exceeds_max_item_count_no_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.max_quantity_per_item = 10
    base_mandate.max_item_count = 3
    base_proposal.quantity = 4
    base_mandate.require_user_approval = False
    res = evaluate_p07_quantity(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK
    assert "max total item count" in res.reason


def test_p07_quantity_exceeds_max_item_count_with_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_mandate.max_quantity_per_item = 10
    base_mandate.max_item_count = 3
    base_proposal.quantity = 4
    base_mandate.require_user_approval = True
    res = evaluate_p07_quantity(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.REQUIRE_APPROVAL
    assert "max total item count" in res.reason


# --- P08 Currency Consistency Tests ---
def test_p08_currency_match(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    res = evaluate_p08_currency(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.PASS


def test_p08_currency_mismatch_blocks_even_with_approval(
    base_mandate: Mandate, base_proposal: Proposal, base_context: EvaluationContext
) -> None:
    base_proposal.currency = "EUR"
    base_mandate.require_user_approval = True
    res = evaluate_p08_currency(base_mandate, base_proposal, base_context)
    assert res.status == RuleStatus.BLOCK
