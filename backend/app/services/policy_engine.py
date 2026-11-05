"""Deterministic Policy Engine for LEASH (Phase 2C).

Evaluates purchase proposals against active signed user mandates without calling
LLMs, external APIs, or executing payments.
"""

import hashlib
import hmac
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse

import aiosqlite

from app.core.config import settings
from app.core.security import (
    CONTROL_CHAR_PATTERN,
    format_money,
    proposal_content_hash,
    sign_payload,
)
from app.db.database import (
    consume_approval_token_in_db,
    hash_approval_token,
    record_approval_token_in_db,
)
from app.schemas.mandate import (
    MERCHANT_IDENTIFIER_REGEX,
    SUPPORTED_CURRENCIES,
    Mandate,
    MandateStatus,
)
from app.schemas.policy import (
    EvaluationContext,
    OracleResult,
    PolicyDecision,
    RuleResult,
    RuleStatus,
    aggregate_decision,
)
from app.schemas.proposal import Proposal

PRICE_TOLERANCE = Decimal("0.05")
ORACLE_STALENESS_THRESHOLD = timedelta(minutes=5)

INJECTION_PATTERNS = (
    "ignore previous",
    "system:",
    "[inst]",
    "<|im_start|>",
    "<script",
    "</script",
    "<iframe",
    "javascript:",
    "--",
    "/*",
    "*/",
    "; drop",
    "; select",
)


def is_ssrf_prohibited_host(url: str) -> bool:
    """Check if URL host targets loopback, private, link-local, or cloud metadata."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower()
        if not hostname:
            return True
        if hostname == "localhost":
            return True
        try:
            ip = ipaddress.ip_address(hostname)
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return True
        except ValueError:
            if hostname in {"metadata.google.internal", "instance-data"}:
                return True
        return False
    except Exception:
        return True


def canonicalize_merchant_domain(raw_merchant: str) -> tuple[str | None, str | None]:
    """Normalize and validate a merchant identifier using IDNA and strict syntax checks.

    Args:
        raw_merchant: Raw merchant string from proposal or mandate.

    Returns:
        Tuple of (canonical_merchant, error_message). If invalid, canonical_merchant is None.
    """
    if not isinstance(raw_merchant, str):
        return None, "Merchant identifier must be a string."

    m = raw_merchant.strip()
    if not m:
        return None, "Merchant identifier cannot be empty."

    if "://" in m:
        return None, "Malformed merchant identifier: URI scheme is forbidden."
    if "/" in m:
        return None, "Malformed merchant identifier: URI path is forbidden."
    if "@" in m:
        return None, "Malformed merchant identifier: credentials are forbidden."
    if ":" in m:
        return None, "Malformed merchant identifier: port numbers are forbidden."
    if " " in m:
        return None, "Malformed merchant identifier: whitespace is forbidden."

    m_lower = m.lower()

    # IDNA encode internationalized domain names (e.g. bücher.de -> xn--bcher-kva.de)
    try:
        m_ascii = m_lower.encode("idna").decode("ascii")
    except Exception as exc:
        return None, f"Invalid IDNA domain encoding: {exc}"

    if not MERCHANT_IDENTIFIER_REGEX.match(m_ascii):
        return None, f"Malformed merchant identifier '{raw_merchant}'."

    return m_ascii, None


def evaluate_p01_signature(mandate: Mandate, context: EvaluationContext) -> RuleResult:
    """P01 — Mandate signature validity (Hard Block)."""
    now = context.now.astimezone(timezone.utc)

    if not mandate.signature:
        return RuleResult(
            rule_id="P01",
            rule_name="Mandate Signature Validity",
            status=RuleStatus.BLOCK,
            reason="Mandate has no cryptographic signature.",
            evidence={"mandate_id": mandate.mandate_id, "signature": None},
            evaluated_at=now,
        )

    if not mandate.signed_at:
        return RuleResult(
            rule_id="P01",
            rule_name="Mandate Signature Validity",
            status=RuleStatus.BLOCK,
            reason="Mandate is missing signed_at timestamp.",
            evidence={"mandate_id": mandate.mandate_id, "signed_at": None},
            evaluated_at=now,
        )

    if mandate.signature_algorithm != "HMAC-SHA256":
        return RuleResult(
            rule_id="P01",
            rule_name="Mandate Signature Validity",
            status=RuleStatus.BLOCK,
            reason=(
                f"Unsupported signature algorithm '{mandate.signature_algorithm}'. "
                "Only HMAC-SHA256 is supported."
            ),
            evidence={
                "mandate_id": mandate.mandate_id,
                "signature_algorithm": mandate.signature_algorithm,
            },
            evaluated_at=now,
        )

    if not settings.MANDATE_SIGNING_KEY:
        return RuleResult(
            rule_id="P01",
            rule_name="Mandate Signature Validity",
            status=RuleStatus.BLOCK,
            reason="Mandate signing key is empty or not configured.",
            evidence={"mandate_id": mandate.mandate_id},
            evaluated_at=now,
        )

    if mandate.signed_at > now:
        return RuleResult(
            rule_id="P01",
            rule_name="Mandate Signature Validity",
            status=RuleStatus.BLOCK,
            reason="Mandate signing timestamp is in the future.",
            evidence={
                "mandate_id": mandate.mandate_id,
                "signed_at": mandate.signed_at.isoformat(),
                "now": now.isoformat(),
            },
            evaluated_at=now,
        )

    try:
        expected = sign_payload(mandate.canonical_payload(), settings.MANDATE_SIGNING_KEY)
    except Exception as exc:
        return RuleResult(
            rule_id="P01",
            rule_name="Mandate Signature Validity",
            status=RuleStatus.BLOCK,
            reason=f"Failed to compute expected mandate signature: {exc}",
            evidence={"mandate_id": mandate.mandate_id},
            evaluated_at=now,
        )

    if not hmac.compare_digest(expected, mandate.signature):
        return RuleResult(
            rule_id="P01",
            rule_name="Mandate Signature Validity",
            status=RuleStatus.BLOCK,
            reason="Cryptographic signature mismatch; mandate may be forged or tampered.",
            evidence={"mandate_id": mandate.mandate_id},
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P01",
        rule_name="Mandate Signature Validity",
        status=RuleStatus.PASS,
        reason="Mandate cryptographic signature is valid and verified.",
        evidence={
            "mandate_id": mandate.mandate_id,
            "signature_algorithm": mandate.signature_algorithm or "HMAC-SHA256",
        },
        evaluated_at=now,
    )


def evaluate_p02_expiration(mandate: Mandate, context: EvaluationContext) -> RuleResult:
    """P02 — Mandate expiration and active status (Hard Block)."""
    now = context.now.astimezone(timezone.utc)

    if mandate.status != MandateStatus.ACTIVE:
        return RuleResult(
            rule_id="P02",
            rule_name="Mandate Expiration and Active Status",
            status=RuleStatus.BLOCK,
            reason=f"Mandate status is '{mandate.status.value}', expected 'ACTIVE'.",
            evidence={"mandate_id": mandate.mandate_id, "status": mandate.status.value},
            evaluated_at=now,
        )

    if mandate.expires_at is None or mandate.expires_at <= now:
        exp_iso = mandate.expires_at.isoformat() if mandate.expires_at else None
        return RuleResult(
            rule_id="P02",
            rule_name="Mandate Expiration and Active Status",
            status=RuleStatus.BLOCK,
            reason="Mandate has expired.",
            evidence={
                "mandate_id": mandate.mandate_id,
                "expires_at": exp_iso,
                "now": now.isoformat(),
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P02",
        rule_name="Mandate Expiration and Active Status",
        status=RuleStatus.PASS,
        reason="Mandate is in ACTIVE lifecycle status and currently unexpired.",
        evidence={
            "mandate_id": mandate.mandate_id,
            "expires_at": mandate.expires_at.isoformat(),
        },
        evaluated_at=now,
    )


def evaluate_p03_budget(
    mandate: Mandate, proposal: Proposal, context: EvaluationContext
) -> RuleResult:
    """P03 — Total amount within mandate budget ceiling (Hard Block)."""
    now = context.now.astimezone(timezone.utc)

    if mandate.max_total_budget is None or mandate.max_total_budget <= Decimal("0.00"):
        return RuleResult(
            rule_id="P03",
            rule_name="Mandate Budget Ceiling",
            status=RuleStatus.BLOCK,
            reason="Mandate has an invalid or missing budget ceiling.",
            evidence={"mandate_id": mandate.mandate_id},
            evaluated_at=now,
        )

    if proposal.total is None or proposal.total <= Decimal("0.00"):
        return RuleResult(
            rule_id="P03",
            rule_name="Mandate Budget Ceiling",
            status=RuleStatus.BLOCK,
            reason="Proposal total is non-positive or missing.",
            evidence={"proposal_id": proposal.proposal_id, "total": str(proposal.total)},
            evaluated_at=now,
        )

    # 1. Single proposal limit check
    if proposal.total > mandate.max_total_budget:
        return RuleResult(
            rule_id="P03",
            rule_name="Mandate Budget Ceiling",
            status=RuleStatus.BLOCK,
            reason="Proposal total exceeds the signed mandate maximum budget ceiling.",
            evidence={
                "proposal_total": format_money(proposal.total),
                "max_total_budget": format_money(mandate.max_total_budget),
            },
            evaluated_at=now,
        )

    # 2. Cumulative lifetime spend check against mandate ceiling
    projected_cumulative = context.cumulative_mandate_spent + proposal.total
    if projected_cumulative > mandate.max_total_budget:
        return RuleResult(
            rule_id="P03",
            rule_name="Mandate Budget Ceiling",
            status=RuleStatus.BLOCK,
            reason=(
                "Cumulative spend plus proposal total exceeds mandate maximum budget ceiling."
            ),
            evidence={
                "cumulative_spent": format_money(context.cumulative_mandate_spent),
                "proposal_total": format_money(proposal.total),
                "projected_cumulative": format_money(projected_cumulative),
                "max_total_budget": format_money(mandate.max_total_budget),
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P03",
        rule_name="Mandate Budget Ceiling",
        status=RuleStatus.PASS,
        reason="Proposal total and cumulative spend are within the mandate budget ceiling.",
        evidence={
            "proposal_total": format_money(proposal.total),
            "max_total_budget": format_money(mandate.max_total_budget),
            "cumulative_spent": format_money(context.cumulative_mandate_spent),
        },
        evaluated_at=now,
    )


def evaluate_p04_item_price(
    mandate: Mandate, proposal: Proposal, context: EvaluationContext
) -> RuleResult:
    """P04 — Per-item spending limit (Soft Check)."""
    now = context.now.astimezone(timezone.utc)

    if mandate.max_price_per_item is None:
        return RuleResult(
            rule_id="P04",
            rule_name="Per-Item Spending Limit",
            status=RuleStatus.NOT_APPLICABLE,
            reason="No per-item spending limit configured on mandate.",
            evidence={},
            evaluated_at=now,
        )

    if mandate.max_price_per_item <= Decimal("0.00"):
        return RuleResult(
            rule_id="P04",
            rule_name="Per-Item Spending Limit",
            status=RuleStatus.BLOCK,
            reason="Mandate has an invalid non-positive max_price_per_item limit.",
            evidence={"max_price_per_item": str(mandate.max_price_per_item)},
            evaluated_at=now,
        )

    if proposal.unit_price is None or proposal.unit_price <= Decimal("0.00"):
        return RuleResult(
            rule_id="P04",
            rule_name="Per-Item Spending Limit",
            status=RuleStatus.BLOCK,
            reason="Proposal unit_price is missing or non-positive.",
            evidence={"unit_price": str(proposal.unit_price)},
            evaluated_at=now,
        )

    if proposal.unit_price > mandate.max_price_per_item:
        if mandate.require_user_approval:
            return RuleResult(
                rule_id="P04",
                rule_name="Per-Item Spending Limit",
                status=RuleStatus.REQUIRE_APPROVAL,
                reason=(
                    f"Unit price ({format_money(proposal.unit_price)}) exceeds mandate limit "
                    f"({format_money(mandate.max_price_per_item)}); requires user approval."
                ),
                evidence={
                    "unit_price": format_money(proposal.unit_price),
                    "max_price_per_item": format_money(mandate.max_price_per_item),
                },
                evaluated_at=now,
            )
        return RuleResult(
            rule_id="P04",
            rule_name="Per-Item Spending Limit",
            status=RuleStatus.BLOCK,
            reason=(
                f"Unit price ({format_money(proposal.unit_price)}) exceeds mandate limit "
                f"({format_money(mandate.max_price_per_item)})."
            ),
            evidence={
                "unit_price": format_money(proposal.unit_price),
                "max_price_per_item": format_money(mandate.max_price_per_item),
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P04",
        rule_name="Per-Item Spending Limit",
        status=RuleStatus.PASS,
        reason="Unit price is within the per-item limit.",
        evidence={
            "unit_price": format_money(proposal.unit_price),
            "max_price_per_item": format_money(mandate.max_price_per_item),
        },
        evaluated_at=now,
    )


def evaluate_p05_merchant(
    mandate: Mandate, proposal: Proposal, context: EvaluationContext
) -> RuleResult:
    """P05 — Merchant allowlist validation (Soft Check)."""
    now = context.now.astimezone(timezone.utc)

    if not mandate.allowed_merchants:
        return RuleResult(
            rule_id="P05",
            rule_name="Merchant Allowlist Validation",
            status=RuleStatus.BLOCK,
            reason="Mandate has an empty merchant allowlist; no merchants permitted.",
            evidence={"allowed_merchants": []},
            evaluated_at=now,
        )

    canonical_m, error = canonicalize_merchant_domain(proposal.merchant)
    if canonical_m is None:
        return RuleResult(
            rule_id="P05",
            rule_name="Merchant Allowlist Validation",
            status=RuleStatus.BLOCK,
            reason=f"Proposal merchant identifier is invalid: {error}",
            evidence={"merchant": proposal.merchant},
            evaluated_at=now,
        )

    # Allowed merchants are already canonicalized in MandateBase validator
    if canonical_m in mandate.allowed_merchants:
        return RuleResult(
            rule_id="P05",
            rule_name="Merchant Allowlist Validation",
            status=RuleStatus.PASS,
            reason=f"Merchant '{canonical_m}' is present in the mandate allowlist.",
            evidence={"merchant": canonical_m},
            evaluated_at=now,
        )

    if mandate.require_user_approval:
        return RuleResult(
            rule_id="P05",
            rule_name="Merchant Allowlist Validation",
            status=RuleStatus.REQUIRE_APPROVAL,
            reason=(
                f"Merchant '{canonical_m}' is not in mandate allowlist; "
                "requires one-time user exception."
            ),
            evidence={
                "merchant": canonical_m,
                "allowed_merchants": mandate.allowed_merchants,
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P05",
        rule_name="Merchant Allowlist Validation",
        status=RuleStatus.BLOCK,
        reason=f"Merchant '{canonical_m}' is not in mandate allowlist.",
        evidence={
            "merchant": canonical_m,
            "allowed_merchants": mandate.allowed_merchants,
        },
        evaluated_at=now,
    )


def evaluate_p06_category(
    mandate: Mandate, proposal: Proposal, context: EvaluationContext
) -> RuleResult:
    """P06 — Product category allowlist validation (Soft Check)."""
    now = context.now.astimezone(timezone.utc)

    if not mandate.allowed_categories:
        return RuleResult(
            rule_id="P06",
            rule_name="Category Allowlist Validation",
            status=RuleStatus.BLOCK,
            reason="Mandate permits no categories (empty allowlist).",
            evidence={"allowed_categories": []},
            evaluated_at=now,
        )

    cat_norm = proposal.category.strip().lower()
    if not cat_norm:
        return RuleResult(
            rule_id="P06",
            rule_name="Category Allowlist Validation",
            status=RuleStatus.BLOCK,
            reason="Proposal category is empty.",
            evidence={"category": proposal.category},
            evaluated_at=now,
        )

    if cat_norm in mandate.allowed_categories:
        return RuleResult(
            rule_id="P06",
            rule_name="Category Allowlist Validation",
            status=RuleStatus.PASS,
            reason=f"Category '{cat_norm}' is in the mandate allowlist.",
            evidence={"category": cat_norm},
            evaluated_at=now,
        )

    if mandate.require_user_approval:
        return RuleResult(
            rule_id="P06",
            rule_name="Category Allowlist Validation",
            status=RuleStatus.REQUIRE_APPROVAL,
            reason=(
                f"Category '{cat_norm}' is not in mandate allowlist; "
                "requires one-time user exception."
            ),
            evidence={
                "category": cat_norm,
                "allowed_categories": mandate.allowed_categories,
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P06",
        rule_name="Category Allowlist Validation",
        status=RuleStatus.BLOCK,
        reason=f"Category '{cat_norm}' is not in mandate allowlist.",
        evidence={
            "category": cat_norm,
            "allowed_categories": mandate.allowed_categories,
        },
        evaluated_at=now,
    )


def evaluate_p07_quantity(
    mandate: Mandate, proposal: Proposal, context: EvaluationContext
) -> RuleResult:
    """P07 — Quantity restrictions (Soft Check)."""
    now = context.now.astimezone(timezone.utc)

    if proposal.quantity < 1:
        return RuleResult(
            rule_id="P07",
            rule_name="Quantity Restrictions",
            status=RuleStatus.BLOCK,
            reason=f"Invalid proposal quantity '{proposal.quantity}', must be >= 1.",
            evidence={"quantity": proposal.quantity},
            evaluated_at=now,
        )

    exceeds_item = (
        mandate.max_quantity_per_item is not None
        and proposal.quantity > mandate.max_quantity_per_item
    )
    exceeds_count = (
        mandate.max_item_count is not None
        and proposal.quantity > mandate.max_item_count
    )

    if not exceeds_item and not exceeds_count:
        return RuleResult(
            rule_id="P07",
            rule_name="Quantity Restrictions",
            status=RuleStatus.PASS,
            reason="Proposal quantity is within configured per-item and total limits.",
            evidence={
                "quantity": proposal.quantity,
                "max_quantity_per_item": mandate.max_quantity_per_item,
                "max_item_count": mandate.max_item_count,
            },
            evaluated_at=now,
        )

    exceeded_desc = []
    if exceeds_item:
        exceeded_desc.append(
            f"per-item limit ({proposal.quantity} > {mandate.max_quantity_per_item})"
        )
    if exceeds_count:
        exceeded_desc.append(
            f"max total item count ({proposal.quantity} > {mandate.max_item_count})"
        )

    reason_str = f"Quantity exceeds {', '.join(exceeded_desc)}."

    if mandate.require_user_approval:
        return RuleResult(
            rule_id="P07",
            rule_name="Quantity Restrictions",
            status=RuleStatus.REQUIRE_APPROVAL,
            reason=f"{reason_str} Requires user approval.",
            evidence={
                "quantity": proposal.quantity,
                "max_quantity_per_item": mandate.max_quantity_per_item,
                "max_item_count": mandate.max_item_count,
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P07",
        rule_name="Quantity Restrictions",
        status=RuleStatus.BLOCK,
        reason=reason_str,
        evidence={
            "quantity": proposal.quantity,
            "max_quantity_per_item": mandate.max_quantity_per_item,
            "max_item_count": mandate.max_item_count,
        },
        evaluated_at=now,
    )


def evaluate_p08_currency(
    mandate: Mandate, proposal: Proposal, context: EvaluationContext
) -> RuleResult:
    """P08 — Currency consistency (Hard Block)."""
    now = context.now.astimezone(timezone.utc)

    if not proposal.currency or proposal.currency not in SUPPORTED_CURRENCIES:
        return RuleResult(
            rule_id="P08",
            rule_name="Currency Consistency",
            status=RuleStatus.BLOCK,
            reason=f"Proposal currency '{proposal.currency}' is missing or unsupported.",
            evidence={"currency": proposal.currency},
            evaluated_at=now,
        )

    if not mandate.currency or mandate.currency not in SUPPORTED_CURRENCIES:
        return RuleResult(
            rule_id="P08",
            rule_name="Currency Consistency",
            status=RuleStatus.BLOCK,
            reason=f"Mandate currency '{mandate.currency}' is missing or unsupported.",
            evidence={"mandate_currency": mandate.currency},
            evaluated_at=now,
        )

    if proposal.currency != mandate.currency:
        return RuleResult(
            rule_id="P08",
            rule_name="Currency Consistency",
            status=RuleStatus.BLOCK,
            reason=(
                f"Proposal currency '{proposal.currency}' does not match "
                f"mandate currency '{mandate.currency}'."
            ),
            evidence={
                "proposal_currency": proposal.currency,
                "mandate_currency": mandate.currency,
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P08",
        rule_name="Currency Consistency",
        status=RuleStatus.PASS,
        reason=f"Currency '{proposal.currency}' matches mandate currency.",
        evidence={"currency": proposal.currency},
        evaluated_at=now,
    )


def evaluate_p09_live_price(
    mandate: Mandate, proposal: Proposal, context: EvaluationContext
) -> RuleResult:
    """P09 — Live price validation & tolerance (Soft Check)."""
    now = context.now.astimezone(timezone.utc)
    oracle = context.oracle_result

    if oracle is None:
        return RuleResult(
            rule_id="P09",
            rule_name="Live Price Validation",
            status=RuleStatus.NOT_APPLICABLE,
            reason="No live price oracle configured.",
            evidence={"product_id": proposal.product_id},
            evaluated_at=now,
        )

    if not oracle.is_available:
        return RuleResult(
            rule_id="P09",
            rule_name="Live Price Validation",
            status=RuleStatus.NOT_APPLICABLE,
            reason="Live price oracle service is currently unavailable.",
            evidence={"product_id": proposal.product_id, "is_available": False},
            evaluated_at=now,
        )

    # Check staleness explicitly via flag or timestamp difference
    age = now - oracle.fetched_at.astimezone(timezone.utc)
    if oracle.is_stale or age > ORACLE_STALENESS_THRESHOLD:
        return RuleResult(
            rule_id="P09",
            rule_name="Live Price Validation",
            status=RuleStatus.NOT_APPLICABLE,
            reason="Live price oracle data is stale (> 5 minutes old).",
            evidence={
                "product_id": proposal.product_id,
                "fetched_at": oracle.fetched_at.isoformat(),
                "age_seconds": age.total_seconds(),
            },
            evaluated_at=now,
        )

    if oracle.live_price is None:
        return RuleResult(
            rule_id="P09",
            rule_name="Live Price Validation",
            status=RuleStatus.NOT_APPLICABLE,
            reason="Product SKU is not indexed in the live price oracle.",
            evidence={"product_id": proposal.product_id},
            evaluated_at=now,
        )

    if oracle.live_price <= Decimal("0.00"):
        return RuleResult(
            rule_id="P09",
            rule_name="Live Price Validation",
            status=RuleStatus.BLOCK,
            reason="Live price oracle returned an invalid non-positive price.",
            evidence={
                "product_id": proposal.product_id,
                "oracle_price": str(oracle.live_price),
            },
            evaluated_at=now,
        )

    delta = abs(proposal.unit_price - oracle.live_price)
    variance = delta / oracle.live_price

    if variance <= PRICE_TOLERANCE:
        return RuleResult(
            rule_id="P09",
            rule_name="Live Price Validation",
            status=RuleStatus.PASS,
            reason=f"Unit price variance is within tolerance ({variance:.2%}).",
            evidence={
                "unit_price": format_money(proposal.unit_price),
                "oracle_price": format_money(oracle.live_price),
                "variance": f"{variance:.4f}",
                "tolerance": str(PRICE_TOLERANCE),
            },
            evaluated_at=now,
        )

    variance_pct = f"{variance:.2%}"
    if mandate.require_user_approval:
        return RuleResult(
            rule_id="P09",
            rule_name="Live Price Validation",
            status=RuleStatus.REQUIRE_APPROVAL,
            reason=(
                f"Unit price variance ({variance_pct}) exceeds {PRICE_TOLERANCE:.0%} tolerance; "
                "requires user approval."
            ),
            evidence={
                "unit_price": format_money(proposal.unit_price),
                "oracle_price": format_money(oracle.live_price),
                "variance": f"{variance:.4f}",
                "tolerance": str(PRICE_TOLERANCE),
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P09",
        rule_name="Live Price Validation",
        status=RuleStatus.BLOCK,
        reason=f"Unit price variance ({variance_pct}) exceeds {PRICE_TOLERANCE:.0%} tolerance.",
        evidence={
            "unit_price": format_money(proposal.unit_price),
            "oracle_price": format_money(oracle.live_price),
            "variance": f"{variance:.4f}",
            "tolerance": str(PRICE_TOLERANCE),
        },
        evaluated_at=now,
    )


def revalidate_checkout_price(
    proposal: Proposal,
    oracle_result: OracleResult | None,
    now: datetime,
    tolerance: Decimal = PRICE_TOLERANCE,
) -> tuple[bool, str]:
    """Revalidate that the proposal price has not drifted beyond tolerance at checkout.

    Args:
        proposal: The proposal awaiting execution.
        oracle_result: Fresh oracle reading captured immediately before checkout.
        now: Current UTC timestamp.
        tolerance: Maximum allowable price drift.

    Returns:
        Tuple of (is_valid, reason).
    """
    if oracle_result is None or not oracle_result.is_available:
        return True, "No live oracle available for checkout revalidation; bypassing."

    age = now.astimezone(timezone.utc) - oracle_result.fetched_at.astimezone(timezone.utc)
    if oracle_result.is_stale or age > ORACLE_STALENESS_THRESHOLD:
        return True, "Oracle data stale at checkout; bypassing."

    if oracle_result.live_price is None or oracle_result.live_price <= Decimal("0.00"):
        return True, "Oracle price missing or invalid at checkout; bypassing."

    delta = abs(proposal.unit_price - oracle_result.live_price)
    variance = delta / oracle_result.live_price

    if variance > tolerance:
        return (
            False,
            f"Price drifted by {variance:.2%} exceeding tolerance of {tolerance:.0%}.",
        )

    return True, f"Price revalidated successfully (drift: {variance:.2%})."


def evaluate_p10_daily_limit(
    mandate: Mandate, proposal: Proposal, context: EvaluationContext
) -> RuleResult:
    """P10 — Daily spend reservation & limit (Soft Check)."""
    now = context.now.astimezone(timezone.utc)

    if context.daily_txn_limit is None:
        return RuleResult(
            rule_id="P10",
            rule_name="Daily Spend Reservation",
            status=RuleStatus.NOT_APPLICABLE,
            reason="No daily transaction limit configured.",
            evidence={"proposal_total": format_money(proposal.total)},
            evaluated_at=now,
        )

    if context.today_reserved < Decimal("0.00"):
        return RuleResult(
            rule_id="P10",
            rule_name="Daily Spend Reservation",
            status=RuleStatus.BLOCK,
            reason="Invalid negative today_reserved spend data.",
            evidence={"today_reserved": str(context.today_reserved)},
            evaluated_at=now,
        )

    projected = context.today_reserved + proposal.total

    if projected <= context.daily_txn_limit:
        return RuleResult(
            rule_id="P10",
            rule_name="Daily Spend Reservation",
            status=RuleStatus.PASS,
            reason=(
                f"Projected daily spend ({format_money(projected)}) is within "
                f"daily limit ({format_money(context.daily_txn_limit)})."
            ),
            evidence={
                "today_reserved": format_money(context.today_reserved),
                "proposal_total": format_money(proposal.total),
                "projected_spend": format_money(projected),
                "daily_txn_limit": format_money(context.daily_txn_limit),
            },
            evaluated_at=now,
        )

    if mandate.require_user_approval:
        return RuleResult(
            rule_id="P10",
            rule_name="Daily Spend Reservation",
            status=RuleStatus.REQUIRE_APPROVAL,
            reason=(
                f"Projected daily spend ({format_money(projected)}) exceeds daily limit "
                f"({format_money(context.daily_txn_limit)}); requires user approval."
            ),
            evidence={
                "today_reserved": format_money(context.today_reserved),
                "proposal_total": format_money(proposal.total),
                "projected_spend": format_money(projected),
                "daily_txn_limit": format_money(context.daily_txn_limit),
            },
            evaluated_at=now,
        )

    return RuleResult(
        rule_id="P10",
        rule_name="Daily Spend Reservation",
        status=RuleStatus.BLOCK,
        reason=(
            f"Projected daily spend ({format_money(projected)}) exceeds daily limit "
            f"({format_money(context.daily_txn_limit)})."
        ),
        evidence={
            "today_reserved": format_money(context.today_reserved),
            "proposal_total": format_money(proposal.total),
            "projected_spend": format_money(projected),
            "daily_txn_limit": format_money(context.daily_txn_limit),
        },
        evaluated_at=now,
    )


def evaluate_p11_duplicate(
    proposal: Proposal,
    context: EvaluationContext,
    existing_by_fingerprint: Proposal | None = None,
    existing_by_idempotency_key: Proposal | None = None,
) -> RuleResult:
    """P11 — Duplicate purchase & replay protection (Hard Block)."""
    now = context.now.astimezone(timezone.utc)

    if not proposal.fingerprint:
        return RuleResult(
            rule_id="P11",
            rule_name="Duplicate / Replay Protection",
            status=RuleStatus.BLOCK,
            reason="Proposal is missing its commercial fingerprint.",
            evidence={"proposal_id": proposal.proposal_id},
            evaluated_at=now,
        )

    if existing_by_fingerprint is not None:
        if existing_by_fingerprint.proposal_id == proposal.proposal_id:
            return RuleResult(
                rule_id="P11",
                rule_name="Duplicate / Replay Protection",
                status=RuleStatus.PASS,
                reason="Idempotent replay of existing proposal with identical identity.",
                evidence={
                    "proposal_id": proposal.proposal_id,
                    "fingerprint": proposal.fingerprint,
                    "is_replay": True,
                },
                evaluated_at=now,
            )
        return RuleResult(
            rule_id="P11",
            rule_name="Duplicate / Replay Protection",
            status=RuleStatus.BLOCK,
            reason=(
                "Duplicate proposal terms detected. A proposal with identical commercial "
                f"terms already exists (ID '{existing_by_fingerprint.proposal_id}')."
            ),
            evidence={
                "proposal_id": proposal.proposal_id,
                "existing_proposal_id": existing_by_fingerprint.proposal_id,
                "fingerprint": proposal.fingerprint,
            },
            evaluated_at=now,
        )

    if proposal.idempotency_key:
        if existing_by_idempotency_key is not None:
            if existing_by_idempotency_key.fingerprint == proposal.fingerprint:
                return RuleResult(
                    rule_id="P11",
                    rule_name="Duplicate / Replay Protection",
                    status=RuleStatus.PASS,
                    reason="Idempotent replay matching existing idempotency key and terms.",
                    evidence={
                        "proposal_id": existing_by_idempotency_key.proposal_id,
                        "idempotency_key": proposal.idempotency_key,
                        "is_replay": True,
                    },
                    evaluated_at=now,
                )
            return RuleResult(
                rule_id="P11",
                rule_name="Duplicate / Replay Protection",
                status=RuleStatus.BLOCK,
                reason=(
                    f"Idempotency key '{proposal.idempotency_key}' was reused with "
                    "different proposal terms."
                ),
                evidence={
                    "idempotency_key": proposal.idempotency_key,
                    "existing_proposal_id": existing_by_idempotency_key.proposal_id,
                },
                evaluated_at=now,
            )

    return RuleResult(
        rule_id="P11",
        rule_name="Duplicate / Replay Protection",
        status=RuleStatus.PASS,
        reason="Proposal is unique; no duplicate fingerprint or key conflict.",
        evidence={"fingerprint": proposal.fingerprint},
        evaluated_at=now,
    )


def evaluate_p12_integrity(
    proposal: Proposal,
    context: EvaluationContext,
) -> RuleResult:
    """P12 — Product data integrity, SSRF defense, and prompt-injection classification."""
    now = context.now.astimezone(timezone.utc)

    # Sub-Check P12-A: Content hash verification against DB tampering
    recomputed_hash = proposal_content_hash(proposal.model_dump())
    if not hmac.compare_digest(recomputed_hash, proposal.content_hash):
        return RuleResult(
            rule_id="P12",
            rule_name="Product Integrity & SSRF/Injection Defense",
            status=RuleStatus.BLOCK,
            reason="Proposal content hash mismatch; stored proposal may have been tampered.",
            evidence={
                "proposal_id": proposal.proposal_id,
                "expected_hash": recomputed_hash,
                "stored_hash": proposal.content_hash,
            },
            evaluated_at=now,
        )

    # Sub-Check P12-B: Disallowed control characters
    for field_name, value in [
        ("product_name", proposal.product_name),
        ("product_id", proposal.product_id),
        ("product_source", proposal.product_source),
    ]:
        if not value or not isinstance(value, str):
            return RuleResult(
                rule_id="P12",
                rule_name="Product Integrity & SSRF/Injection Defense",
                status=RuleStatus.BLOCK,
                reason=f"Proposal field '{field_name}' is missing or empty.",
                evidence={"field_name": field_name},
                evaluated_at=now,
            )
        if CONTROL_CHAR_PATTERN.search(value):
            return RuleResult(
                rule_id="P12",
                rule_name="Product Integrity & SSRF/Injection Defense",
                status=RuleStatus.BLOCK,
                reason=f"Disallowed control characters detected in proposal '{field_name}'.",
                evidence={"field_name": field_name},
                evaluated_at=now,
            )

    # Sub-Check P12-B: URI scheme, credentials, and SSRF host checks on product_source
    src = proposal.product_source.strip()
    if not (src.startswith("https://") or src.startswith("http://")):
        return RuleResult(
            rule_id="P12",
            rule_name="Product Integrity & SSRF/Injection Defense",
            status=RuleStatus.BLOCK,
            reason="Proposal product_source must use http:// or https:// scheme.",
            evidence={"product_source": src},
            evaluated_at=now,
        )

    parsed_src = urlparse(src)
    if "@" in (parsed_src.netloc or ""):
        return RuleResult(
            rule_id="P12",
            rule_name="Product Integrity & SSRF/Injection Defense",
            status=RuleStatus.BLOCK,
            reason="Proposal product_source URL contains forbidden embedded credentials.",
            evidence={"product_source": src},
            evaluated_at=now,
        )

    if is_ssrf_prohibited_host(src):
        return RuleResult(
            rule_id="P12",
            rule_name="Product Integrity & SSRF/Injection Defense",
            status=RuleStatus.BLOCK,
            reason="Proposal product_source targets loopback, private, or metadata host (SSRF).",
            evidence={"product_source": src},
            evaluated_at=now,
        )

    # Sub-Check P12-C: Prompt-injection heuristic classification
    for field_name, value in [
        ("product_name", proposal.product_name),
        ("product_id", proposal.product_id),
    ]:
        val_lower = value.lower()
        for marker in INJECTION_PATTERNS:
            if marker in val_lower:
                return RuleResult(
                    rule_id="P12",
                    rule_name="Product Integrity & SSRF/Injection Defense",
                    status=RuleStatus.BLOCK,
                    reason=(
                        f"Potential prompt injection marker '{marker}' detected in '{field_name}'."
                    ),
                    evidence={"field_name": field_name, "marker": marker},
                    evaluated_at=now,
                )

    return RuleResult(
        rule_id="P12",
        rule_name="Product Integrity & SSRF/Injection Defense",
        status=RuleStatus.PASS,
        reason="Cryptographic content hash, URL syntax, and untrusted text checks passed.",
        evidence={"proposal_id": proposal.proposal_id},
        evaluated_at=now,
    )


def evaluate_policy(
    proposal: Proposal,
    mandate: Mandate,
    context: EvaluationContext,
    existing_by_fingerprint: Proposal | None = None,
    existing_by_idempotency_key: Proposal | None = None,
) -> PolicyDecision:
    """Evaluate all 12 policy rules for a purchase proposal against a mandate.

    Pure mathematical evaluation without network calls, LLM invocations, or DB mutations.

    Args:
        proposal: The purchase proposal under evaluation.
        mandate: The associated user mandate.
        context: Evaluation context with UTC timestamp, spend reservation, and limits.
        existing_by_fingerprint: Proposal matching fingerprint if already in DB.
        existing_by_idempotency_key: Proposal matching idempotency key if already in DB.

    Returns:
        Structured PolicyDecision containing all 12 rule findings and aggregate outcome.
    """
    now = context.now.astimezone(timezone.utc)

    # Evaluate ALL 12 rules deterministically
    rule_results: list[RuleResult] = [
        evaluate_p01_signature(mandate, context),
        evaluate_p02_expiration(mandate, context),
        evaluate_p03_budget(mandate, proposal, context),
        evaluate_p04_item_price(mandate, proposal, context),
        evaluate_p05_merchant(mandate, proposal, context),
        evaluate_p06_category(mandate, proposal, context),
        evaluate_p07_quantity(mandate, proposal, context),
        evaluate_p08_currency(mandate, proposal, context),
        evaluate_p09_live_price(mandate, proposal, context),
        evaluate_p10_daily_limit(mandate, proposal, context),
        evaluate_p11_duplicate(
            proposal,
            context,
            existing_by_fingerprint=existing_by_fingerprint,
            existing_by_idempotency_key=existing_by_idempotency_key,
        ),
        evaluate_p12_integrity(proposal, context),
    ]

    return aggregate_decision(
        proposal_id=proposal.proposal_id,
        mandate_id=mandate.mandate_id,
        rules=rule_results,
        mandate_require_user_approval=mandate.require_user_approval,
        decision_at=now,
    )


def canonicalize_approval_payload(
    proposal_id: str,
    content_hash: str,
    user_session_id: str,
    approval_timestamp: datetime,
    expires_at: datetime,
    nonce: str,
) -> str:
    """Serialize approval token parameters into deterministic canonical JSON.

    Adheres to the Restricted Deterministic JSON Profile:
    - Rejects missing, empty, or whitespace-only nonce.
    - Deterministically formats UTC timestamps to ISO-8601.
    - Keys are sorted lexicographically with compact separators.
    """
    if not isinstance(nonce, str) or not nonce.strip():
        raise ValueError("nonce is mandatory and must be a non-empty string.")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        raise ValueError("proposal_id is mandatory and must be a non-empty string.")
    if not isinstance(content_hash, str) or not content_hash.strip():
        raise ValueError("content_hash is mandatory and must be a non-empty string.")
    if not isinstance(user_session_id, str) or not user_session_id.strip():
        raise ValueError("user_session_id is mandatory and must be a non-empty string.")

    ts_iso = approval_timestamp.astimezone(timezone.utc).isoformat()
    exp_iso = expires_at.astimezone(timezone.utc).isoformat()

    envelope = {
        "content_hash": content_hash.strip(),
        "expires_at": exp_iso,
        "issued_at": ts_iso,
        "nonce": nonce.strip(),
        "proposal_id": proposal_id.strip(),
        "user_session_id": user_session_id.strip(),
    }
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def generate_approval_token(
    proposal_id: str,
    content_hash: str,
    user_session_id: str,
    approval_timestamp: datetime,
    nonce: str,
    expires_at: datetime | None = None,
    secret_key: str | None = None,
    ttl_seconds: int = 900,
) -> str:
    """Generate cryptographically bound single-use user approval token.

    Binds nonce, proposal ID, content hash, user session, and UTC issuance/expiry.
    """
    key = secret_key or settings.MANDATE_SIGNING_KEY
    if not key:
        raise ValueError("Mandate signing key is required for approval tokens.")
    if expires_at is None:
        expires_at = approval_timestamp.astimezone(timezone.utc) + timedelta(seconds=ttl_seconds)

    payload = canonicalize_approval_payload(
        proposal_id=proposal_id,
        content_hash=content_hash,
        user_session_id=user_session_id,
        approval_timestamp=approval_timestamp,
        expires_at=expires_at,
        nonce=nonce,
    )
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_approval_token(
    token: str,
    proposal_id: str,
    content_hash: str,
    user_session_id: str,
    approval_timestamp: datetime,
    nonce: str,
    now: datetime,
    expires_at: datetime | None = None,
    ttl_seconds: int = 900,
    secret_key: str | None = None,
) -> tuple[bool, str]:
    """Verify approval token is cryptographically valid, not expired, and correctly bound."""
    if not token or not isinstance(token, str):
        return False, "Approval token is missing or empty."
    if not nonce or not isinstance(nonce, str) or not nonce.strip():
        return False, "Approval token nonce is missing or empty."

    appr_utc = approval_timestamp.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)

    if expires_at is None:
        expires_at = appr_utc + timedelta(seconds=ttl_seconds)
    else:
        expires_at = expires_at.astimezone(timezone.utc)

    age_seconds = (now_utc - appr_utc).total_seconds()
    if age_seconds < 0:
        return False, "Approval token timestamp is in the future."
    if now_utc > expires_at:
        return False, f"Approval token has expired ({age_seconds:.0f}s > {ttl_seconds}s TTL)."

    key = secret_key or settings.MANDATE_SIGNING_KEY
    if not key:
        return False, "Mandate signing key is required for approval token verification."

    try:
        expected = generate_approval_token(
            proposal_id=proposal_id,
            content_hash=content_hash,
            user_session_id=user_session_id,
            approval_timestamp=approval_timestamp,
            nonce=nonce,
            expires_at=expires_at,
            secret_key=key,
            ttl_seconds=ttl_seconds,
        )
    except ValueError as exc:
        return False, f"Token generation validation error: {exc}"

    if not hmac.compare_digest(expected, token):
        return False, "Approval token verification failed (mismatch or terms modified)."

    return True, "Approval token verified successfully."


async def register_approval_token(
    token: str,
    proposal_id: str,
    approval_timestamp: datetime,
    ttl_seconds: int = 900,
    db: aiosqlite.Connection | None = None,
) -> None:
    """Pre-register an issued approval token's hash in database for single-use tracking."""
    token_hash = hash_approval_token(token)
    expires_at = approval_timestamp.astimezone(timezone.utc) + timedelta(seconds=ttl_seconds)
    created_at_iso = approval_timestamp.astimezone(timezone.utc).isoformat()
    expires_at_iso = expires_at.isoformat()
    await record_approval_token_in_db(
        token_hash=token_hash,
        proposal_id=proposal_id,
        expires_at_iso=expires_at_iso,
        created_at_iso=created_at_iso,
        db=db,
    )


async def verify_and_consume_approval_token(
    token: str,
    proposal_id: str,
    content_hash: str,
    user_session_id: str,
    approval_timestamp: datetime,
    nonce: str,
    now: datetime,
    expires_at: datetime | None = None,
    ttl_seconds: int = 900,
    secret_key: str | None = None,
    db: aiosqlite.Connection | None = None,
) -> tuple[bool, str]:
    """Verify approval token cryptographically and atomically consume it in database.

    Enforces:
    1. Cryptographic HMAC-SHA256 signature validity over canonical JSON.
    2. Mandatory nonce binding.
    3. Exact proposal binding and un-tampered commercial terms (content_hash).
    4. Expiry check against TTL and UTC issuance timestamp.
    5. Atomic single-use consumption in SQLite (prevents concurrent replay).
    """
    is_valid, reason = verify_approval_token(
        token=token,
        proposal_id=proposal_id,
        content_hash=content_hash,
        user_session_id=user_session_id,
        approval_timestamp=approval_timestamp,
        nonce=nonce,
        now=now,
        expires_at=expires_at,
        ttl_seconds=ttl_seconds,
        secret_key=secret_key,
    )
    if not is_valid:
        return False, reason

    token_hash = hash_approval_token(token)
    consumed_at_iso = now.astimezone(timezone.utc).isoformat()
    return await consume_approval_token_in_db(
        token_hash=token_hash,
        proposal_id=proposal_id,
        consumed_at_iso=consumed_at_iso,
        db=db,
    )
