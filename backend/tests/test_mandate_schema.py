"""Tests for the Mandate schema: validation constraints, Pydantic models, and strict rejection."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.mandate import Mandate, MandateCreate, MandateStatus


def _valid_mandate_data() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "max_total_budget": Decimal("40.00"),
        "currency": "USD",
        "allowed_merchants": ["amazon.com", "target.com"],
        "allowed_categories": ["gifts", "books"],
        "max_item_count": 2,
        "max_quantity_per_item": 1,
        "max_price_per_item": Decimal("25.00"),
        "allow_subscriptions": False,
        "require_user_approval": True,
        "expires_at": now + timedelta(hours=24),
    }


def test_valid_mandate_create() -> None:
    """Test valid mandate payload creates successfully with default values."""
    data = _valid_mandate_data()
    mandate = MandateCreate(**data)
    assert mandate.max_total_budget == Decimal("40.00")
    assert mandate.currency == "USD"
    assert mandate.allowed_merchants == ["amazon.com", "target.com"]
    assert mandate.allowed_categories == ["books", "gifts"]  # sorted
    assert mandate.max_item_count == 2
    assert mandate.max_quantity_per_item == 1
    assert mandate.max_price_per_item == Decimal("25.00")
    assert mandate.allow_subscriptions is False
    assert mandate.require_user_approval is True


@pytest.mark.parametrize(
    "invalid_budget",
    [
        Decimal("0.00"),
        Decimal("-10.00"),
        Decimal("40.005"),  # Exceeds 2 decimal places
        Decimal("-0.01"),
    ],
)
def test_invalid_budget_rejection(invalid_budget: Decimal) -> None:
    """Test non-positive or overly precise budgets are rejected."""
    data = _valid_mandate_data()
    data["max_total_budget"] = invalid_budget
    with pytest.raises(ValidationError):
        MandateCreate(**data)


@pytest.mark.parametrize(
    "invalid_currency",
    [
        "usd",  # lowercase
        "US",  # 2 characters
        "USDD",  # 4 characters
        "INVALID",
        "BTC",  # Unsupported currency
        123,
    ],
)
def test_invalid_currency_rejection(invalid_currency: object) -> None:
    """Test invalid currency strings or types are rejected."""
    data = _valid_mandate_data()
    data["currency"] = invalid_currency
    with pytest.raises(ValidationError):
        MandateCreate(**data)


@pytest.mark.parametrize(
    "invalid_merchants",
    [
        [],  # Empty list
        ["https://amazon.com"],  # URL protocol included
        ["http://target.com/path"],  # URL path included
        ["amazon .com"],  # Spaces in domain
        [""],  # Empty string
        ["amazon/com"],  # Path separator
    ],
)
def test_invalid_merchant_restrictions(invalid_merchants: list) -> None:
    """Test malformed merchant domains or empty lists are rejected."""
    data = _valid_mandate_data()
    data["allowed_merchants"] = invalid_merchants
    with pytest.raises(ValidationError):
        MandateCreate(**data)


def test_empty_categories_rejection() -> None:
    """Test that empty allowed_categories list is rejected."""
    data = _valid_mandate_data()
    data["allowed_categories"] = []
    with pytest.raises(ValidationError):
        MandateCreate(**data)


def test_invalid_expiration_in_the_past() -> None:
    """Test that expiration timestamps in the past or before created_at are rejected."""
    now = datetime.now(timezone.utc)
    data = _valid_mandate_data()
    data["expires_at"] = now - timedelta(hours=1)
    with pytest.raises(ValidationError) as exc_info:
        MandateCreate(**data)
    assert "expires_at must be strictly after created_at" in str(exc_info.value)


def test_naive_datetime_rejection() -> None:
    """Test that timezone-naive datetimes are rejected."""
    data = _valid_mandate_data()
    data["expires_at"] = datetime(2027, 1, 1, 12, 0)  # No tzinfo
    with pytest.raises(ValidationError) as exc_info:
        MandateCreate(**data)
    assert "timezone-aware" in str(exc_info.value)


def test_missing_required_fields() -> None:
    """Test that omitting required fields raises ValidationError."""
    with pytest.raises(ValidationError):
        MandateCreate(max_total_budget=Decimal("40.00"))


def test_unexpected_fields_rejected() -> None:
    """Test that unexpected or injected fields are rejected (extra='forbid')."""
    data = _valid_mandate_data()
    data["unauthorized_override"] = "true"
    with pytest.raises(ValidationError) as exc_info:
        MandateCreate(**data)
    assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not permitted" in str(
        exc_info.value
    )


def test_max_quantity_exceeds_item_count_rejected() -> None:
    """Test that per-item quantity cannot exceed total item count."""
    data = _valid_mandate_data()
    data["max_item_count"] = 2
    data["max_quantity_per_item"] = 3
    with pytest.raises(ValidationError) as exc_info:
        MandateCreate(**data)
    assert "max_quantity_per_item cannot exceed max_item_count" in str(exc_info.value)


def test_per_item_price_exceeds_total_budget_rejected() -> None:
    """Test that max_price_per_item cannot exceed max_total_budget."""
    data = _valid_mandate_data()
    data["max_total_budget"] = Decimal("40.00")
    data["max_price_per_item"] = Decimal("45.00")
    with pytest.raises(ValidationError) as exc_info:
        MandateCreate(**data)
    assert "max_price_per_item cannot exceed max_total_budget" in str(exc_info.value)


def test_merchant_and_category_dedup_and_sort() -> None:
    """Test merchant/category lists are lowercased, deduplicated, and sorted."""
    data = _valid_mandate_data()
    data["allowed_merchants"] = ["Target.com", "amazon.com", "AMAZON.COM"]
    data["allowed_categories"] = ["Gifts", "books", "BOOKS"]
    mandate = MandateCreate(**data)
    assert mandate.allowed_merchants == ["amazon.com", "target.com"]
    assert mandate.allowed_categories == ["books", "gifts"]


def test_client_supplied_created_at_rejected() -> None:
    """Test that created_at cannot be injected through the create payload."""
    data = _valid_mandate_data()
    data["created_at"] = datetime.now(timezone.utc)
    with pytest.raises(ValidationError) as exc_info:
        MandateCreate(**data)
    assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not permitted" in str(
        exc_info.value
    )
