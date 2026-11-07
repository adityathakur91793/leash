"""Strict input, output, and persistence schemas for the Intent Auditor."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictAuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IntentRiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNCERTAIN = "UNCERTAIN"


class IntentFindingCode(str, Enum):
    AMBIGUOUS_INTENT = "AMBIGUOUS_INTENT"
    BUDGET_TENSION = "BUDGET_TENSION"
    CATEGORY_MISMATCH = "CATEGORY_MISMATCH"
    CURRENCY_INCONSISTENCY = "CURRENCY_INCONSISTENCY"
    MERCHANT_MISMATCH = "MERCHANT_MISMATCH"
    PRICE_INCONSISTENCY = "PRICE_INCONSISTENCY"
    QUANTITY_INCONSISTENCY = "QUANTITY_INCONSISTENCY"
    SUBSCRIPTION_CONCERN = "SUBSCRIPTION_CONCERN"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"


class IntentAffectedField(str, Enum):
    PRODUCT_NAME = "product_name"
    MERCHANT = "merchant"
    CATEGORY = "category"
    QUANTITY = "quantity"
    UNIT_PRICE = "unit_price"
    TOTAL = "total"
    CURRENCY = "currency"
    MANDATE_BUDGET = "mandate_budget"
    ALLOWED_MERCHANTS = "allowed_merchants"
    ALLOWED_CATEGORIES = "allowed_categories"
    ITEM_LIMITS = "item_limits"
    SUBSCRIPTIONS = "subscriptions"


class IntentAuditorPolicyRule(StrictAuditModel):
    rule_id: str = Field(..., pattern=r"^P(0[1-9]|1[0-2])$")
    status: Literal["PASS", "BLOCK", "REQUIRE_APPROVAL", "NOT_APPLICABLE"]


class IntentMandateSummary(StrictAuditModel):
    currency: str = Field(..., min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    max_total_budget: str = Field(..., min_length=1, max_length=24)
    max_price_per_item: str | None = Field(default=None, max_length=24)
    max_item_count: int = Field(..., ge=1, le=10_000)
    max_quantity_per_item: int = Field(..., ge=1, le=10_000)
    allowed_merchants: list[str] = Field(..., min_length=1, max_length=25)
    allowed_categories: list[str] = Field(..., min_length=1, max_length=25)
    allow_subscriptions: bool
    require_user_approval: bool
    is_active_and_unexpired: Literal[True]


class IntentProposalSummary(StrictAuditModel):
    product_name: str = Field(..., min_length=1, max_length=120)
    merchant: str = Field(..., min_length=1, max_length=128)
    category: str = Field(..., min_length=1, max_length=64)
    quantity: int = Field(..., ge=1, le=10_000)
    unit_price: str = Field(..., min_length=1, max_length=24)
    total: str = Field(..., min_length=1, max_length=24)
    currency: str = Field(..., min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class IntentAuditorInput(StrictAuditModel):
    """Minimum necessary structured input; contains no internal IDs or credentials."""

    mandate: IntentMandateSummary
    proposal: IntentProposalSummary
    deterministic_outcome: Literal["ALLOW", "REQUIRE_APPROVAL"]
    policy_rules: list[IntentAuditorPolicyRule] = Field(..., min_length=12, max_length=12)


class IntentAuditorResult(StrictAuditModel):
    """Advisory risk assessment. This schema intentionally has no decision field."""

    schema_version: Literal["1.0"]
    risk_level: IntentRiskLevel
    finding_codes: list[IntentFindingCode] = Field(default_factory=list, max_length=8)
    affected_fields: list[IntentAffectedField] = Field(default_factory=list, max_length=8)

    @field_validator("finding_codes", "affected_fields")
    @classmethod
    def reject_duplicates(cls, values: list[Enum]) -> list[Enum]:
        if len(values) != len(set(values)):
            raise ValueError("Assessment lists cannot contain duplicate values.")
        return values


class IntentAssessmentStatus(str, Enum):
    VALID = "VALID"
    FAILED = "FAILED"
    STALE = "STALE"


class IntentAssessmentRecord(StrictAuditModel):
    assessment_id: str
    proposal_id: str
    mandate_id: str
    status: IntentAssessmentStatus
    risk_level: IntentRiskLevel
    finding_codes: list[IntentFindingCode]
    affected_fields: list[IntentAffectedField]
    proposal_content_hash: str = Field(..., min_length=64, max_length=64)
    mandate_permissions_hash: str = Field(..., min_length=64, max_length=64)
    policy_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    policy_version: str
    schema_version: str
    prompt_version: str
    model_identifier: str
    failure_code: str | None = None
    created_at: str

