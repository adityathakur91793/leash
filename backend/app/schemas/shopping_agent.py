"""Strict product and Shopping Agent API schemas."""

from decimal import Decimal
from enum import Enum
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.mandate import MERCHANT_IDENTIFIER_REGEX, SUPPORTED_CURRENCIES
from app.schemas.authorization import PublicPolicyDecision
from app.schemas.proposal import MAX_QUANTITY, ProposalStatus

PRODUCT_ID_PATTERN = re.compile(r"^demo_[a-z0-9][a-z0-9_-]{0,62}$")
MAX_CATALOGUE_PRICE = Decimal("999999999.99")


class ProductDataStatus(str, Enum):
    SIMULATED = "SIMULATED"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"
    QUARANTINED = "QUARANTINED"


class SimulatedAvailability(str, Enum):
    SIMULATED_AVAILABLE = "SIMULATED_AVAILABLE"
    SIMULATED_UNAVAILABLE = "SIMULATED_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductRecord(StrictSchema):
    """Catalogue record. All prices and availability are explicitly simulated."""

    product_id: str = Field(..., min_length=6, max_length=68)
    name: str = Field(..., min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=600)
    description_status: ProductDataStatus
    merchant_id: str = Field(..., min_length=1, max_length=128)
    merchant_name: str = Field(..., min_length=1, max_length=100)
    category: str = Field(..., min_length=1, max_length=64)
    unit_price: Decimal | None = None
    tax: Decimal | None = None
    shipping: Decimal | None = None
    other_charges: Decimal | None = None
    currency: str | None = None
    price_status: ProductDataStatus
    availability: SimulatedAvailability
    availability_status: ProductDataStatus
    source: str = Field(default="LOCAL_SIMULATED_CATALOG", max_length=64)
    is_simulated: bool = True

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str) -> str:
        if not PRODUCT_ID_PATTERN.fullmatch(value):
            raise ValueError("Invalid stable catalogue product ID.")
        return value

    @field_validator("merchant_id")
    @classmethod
    def validate_merchant_id(cls, value: str) -> str:
        canonical = value.lower()
        if not MERCHANT_IDENTIFIER_REGEX.fullmatch(canonical):
            raise ValueError("Invalid canonical merchant identifier.")
        return canonical

    @field_validator("category")
    @classmethod
    def normalize_category(cls, value: str) -> str:
        return value.lower()

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_CURRENCIES:
            raise ValueError("Unsupported currency.")
        return value

    @field_validator("unit_price", "tax", "shipping", "other_charges")
    @classmethod
    def validate_money(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return value
        if (
            not value.is_finite()
            or value < 0
            or value > MAX_CATALOGUE_PRICE
            or value.as_tuple().exponent < -2
        ):
            raise ValueError("Money values must be finite, non-negative, and at most 2 decimals.")
        return value.quantize(Decimal("0.01"))

    @model_validator(mode="after")
    def enforce_simulation_disclosure(self) -> "ProductRecord":
        if not self.is_simulated:
            raise ValueError("This initial catalogue accepts simulated records only.")
        if self.price_status == ProductDataStatus.SIMULATED:
            if self.unit_price is None or self.currency is None:
                raise ValueError("Simulated price requires an amount and currency.")
        elif self.unit_price is not None:
            raise ValueError("A non-simulated price status cannot include an amount.")
        if any(value is not None for value in (self.tax, self.shipping, self.other_charges)):
            if self.unit_price is None:
                raise ValueError("Charges require a known catalogue price.")
        if self.availability_status == ProductDataStatus.SIMULATED:
            if self.availability == SimulatedAvailability.UNKNOWN:
                raise ValueError("Simulated availability requires an explicit simulated state.")
        elif self.availability != SimulatedAvailability.UNKNOWN:
            raise ValueError("Unverified availability must be marked UNKNOWN.")
        return self


class ProductSearchArgs(StrictSchema):
    keyword: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=64)
    merchant: str | None = Field(default=None, max_length=128)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    currency: str | None = None
    limit: int = Field(default=10, ge=1, le=25)

    @field_validator("merchant")
    @classmethod
    def validate_merchant(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.lower()
        if not MERCHANT_IDENTIFIER_REGEX.fullmatch(value):
            raise ValueError("Invalid merchant identifier.")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_CURRENCIES:
            raise ValueError("Unsupported currency.")
        return value

    @field_validator("min_price", "max_price")
    @classmethod
    def validate_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (
            not value.is_finite()
            or value < 0
            or value > MAX_CATALOGUE_PRICE
            or value.as_tuple().exponent < -2
        ):
            raise ValueError("Price filters must be finite, non-negative, and at most 2 decimals.")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "ProductSearchArgs":
        if self.min_price is not None or self.max_price is not None:
            if self.currency is None:
                raise ValueError("Currency is required for price filters.")
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price cannot exceed max_price.")
        return self


class ProductLookupArgs(StrictSchema):
    product_id: str = Field(..., min_length=6, max_length=68)

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str) -> str:
        if not PRODUCT_ID_PATTERN.fullmatch(value):
            raise ValueError("Invalid stable catalogue product ID.")
        return value


class ProposePurchaseArgs(StrictSchema):
    """Only catalogue ID and quantity are model-supplied commercial inputs."""

    product_id: str = Field(..., min_length=6, max_length=68)
    quantity: int = Field(..., ge=1, le=MAX_QUANTITY)

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str) -> str:
        if not PRODUCT_ID_PATTERN.fullmatch(value):
            raise ValueError("Invalid stable catalogue product ID.")
        return value


class ShoppingAgentRequest(StrictSchema):
    message: str = Field(..., min_length=1, max_length=1500)
    mandate_id: str | None = Field(default=None, min_length=1, max_length=200)


class OwnedMandateOption(StrictSchema):
    mandate_id: str
    currency: str
    max_total_budget: Decimal
    expires_at: str


class ProductLookupResult(StrictSchema):
    found: bool
    product: ProductRecord | None = None
    message: str = Field(..., max_length=200)


class ProposalToolResult(StrictSchema):
    proposal_id: str
    status: ProposalStatus
    policy_decision: PublicPolicyDecision
    simulated: bool = True


class ShoppingAgentResponse(StrictSchema):
    message: str = Field(..., min_length=1, max_length=2000)
    products: list[ProductRecord] = Field(default_factory=list, max_length=25)
    proposal: ProposalToolResult | None = None
