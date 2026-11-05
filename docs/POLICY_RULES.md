# LEASH — Policy Rules Summary (Phase 2C)

**Document:** `docs/POLICY_RULES.md`
**Revision:** v2.1 (Security Review V2 Approved)
**Date:** 2026-11-04

This document serves as the quick-reference summary for the twelve policy engine authorization rules (P01 – P12). For detailed architectural specifications and security boundaries, refer to `docs/POLICY_ENGINE_SPEC.md`.

---

## 1. Master Classification Matrix

| Rule ID | Rule Name | Boundary Classification | Overridable by User Approval? | Can Produce `NOT_APPLICABLE`? |
|:---:|---|:---:|:---:|:---:|
| **P01** | Mandate Signature Validity | **Hard Block** | ❌ Never | ❌ No |
| **P02** | Mandate Expiration & Lifecycle | **Hard Block** | ❌ Never | ❌ No |
| **P03** | Total Budget Ceiling | **Hard Block** | ❌ Never | ❌ No |
| **P04** | Per-Item Price Limit | **Soft Check** | ⚠️ Yes (One-time exception) | ✅ Yes (If limit unset) |
| **P05** | Merchant Allowlist Validation | **Soft Check** | ⚠️ Yes (One-time exception) | ❌ No |
| **P06** | Category Allowlist Validation | **Soft Check** | ⚠️ Yes (One-time exception) | ❌ No |
| **P07** | Quantity Restrictions | **Soft Check** | ⚠️ Yes (One-time exception) | ❌ No |
| **P08** | Currency Consistency | **Hard Block** | ❌ Never | ❌ No |
| **P09** | Live Price Validation | **Soft Check** | ⚠️ Yes (User confirms variance) | ✅ Yes (If oracle unavailable/stale) |
| **P10** | Daily Spend Reservation | **Soft Check** | ⚠️ Yes (User confirms daily pace) | ✅ Yes (If limit unset) |
| **P11** | Duplicate / Replay Protection | **Hard Block** | ❌ Never | ❌ No |
| **P12** | Product Integrity & SSRF/Injection | **Hard Block** | ❌ Never | ❌ No |

### Definitions
- **Hard Block:** Absolute security boundary. Any failure immediately produces `BLOCK`. Ordinary user approval can **never** override a hard block.
- **Soft Check:** Parameterized limit. If violated:
  - When `mandate.require_user_approval == True` $\rightarrow$ Produces `REQUIRE_APPROVAL` (isolated, one-time exception).
  - When `mandate.require_user_approval == False` $\rightarrow$ Produces `BLOCK`.
- **NOT_APPLICABLE:** Produced when a rule's prerequisite is explicitly unconfigured or evidence is unavailable (e.g., live-price oracle is down or daily limit is unset). It is logged with evidence and is **never** disguised as a false `PASS`.

---

## 2. Decision Outcomes & Precedence

1. **BLOCK:** Produced if **any** rule evaluates to `BLOCK`. Overrides all other findings.
2. **REQUIRE_APPROVAL:** Produced if no rule is `BLOCK` AND (at least one rule is `REQUIRE_APPROVAL` OR `mandate.require_user_approval == True`).
3. **ALLOW:** Produced **only** if all 12 rules are `PASS` or `NOT_APPLICABLE` AND `mandate.require_user_approval == False`.

*Note:* All 12 rules are **always** evaluated to maintain complete audit logs. Short-circuiting is prohibited.

---

## 3. Quick-Reference Rule Logic

### P01 — Signature Validity
```python
if not mandate.signature or not mandate.signed_at:
    return RuleResult("P01", RuleStatus.BLOCK, "Missing signature or signed_at timestamp")
if mandate.signed_at > context.now:
    return RuleResult("P01", RuleStatus.BLOCK, "Signature timestamp is in the future")
expected = sign_payload(mandate.canonical_payload(), settings.MANDATE_SIGNING_KEY)
if not hmac.compare_digest(expected, mandate.signature):
    return RuleResult("P01", RuleStatus.BLOCK, "Cryptographic signature mismatch")
return RuleResult("P01", RuleStatus.PASS, "Signature verified")
```

### P02 — Expiration & Lifecycle Status
```python
if mandate.status != MandateStatus.ACTIVE:
    return RuleResult("P02", RuleStatus.BLOCK, f"Mandate not ACTIVE (status: {mandate.status})")
if mandate.expires_at <= context.now:
    return RuleResult("P02", RuleStatus.BLOCK, "Mandate has expired")
return RuleResult("P02", RuleStatus.PASS, "Mandate is active and unexpired")
```

### P03 — Mandate Budget Ceiling
```python
if proposal.total > mandate.max_total_budget:
    return RuleResult("P03", RuleStatus.BLOCK, "Proposal total exceeds mandate budget ceiling")
if (context.cumulative_mandate_spent + proposal.total) > mandate.max_total_budget:
    return RuleResult("P03", RuleStatus.BLOCK, "Cumulative spend exceeds mandate budget ceiling")
return RuleResult("P03", RuleStatus.PASS, "Spend within signed mandate ceiling")
```

### P04 — Per-Item Price Limit
```python
if mandate.max_price_per_item is None:
    return RuleResult("P04", RuleStatus.NOT_APPLICABLE, "No max_price_per_item configured")
if proposal.unit_price <= mandate.max_price_per_item:
    return RuleResult("P04", RuleStatus.PASS, "Unit price within limit")
if mandate.require_user_approval:
    return RuleResult("P04", RuleStatus.REQUIRE_APPROVAL, "Unit price exceeds limit; requires approval")
return RuleResult("P04", RuleStatus.BLOCK, "Unit price exceeds limit")
```

### P05 — Merchant Allowlist Validation
```python
# Normalization: lower, strip, IDNA encode, check regex, reject schemes/paths/credentials/ports
m_norm = canonicalize_merchant_domain(proposal.merchant) # Blocks if malformed
if m_norm in mandate.allowed_merchants:
    return RuleResult("P05", RuleStatus.PASS, "Merchant allowed")
if mandate.require_user_approval:
    return RuleResult("P05", RuleStatus.REQUIRE_APPROVAL, "Unlisted merchant; requires one-time exception")
return RuleResult("P05", RuleStatus.BLOCK, "Merchant not in allowlist")
```

### P06 — Category Allowlist Validation
```python
if not mandate.allowed_categories: # Empty list = 0 permitted categories
    return RuleResult("P06", RuleStatus.BLOCK, "Mandate permits no categories")
cat_norm = proposal.category.strip().lower()
if cat_norm in mandate.allowed_categories:
    return RuleResult("P06", RuleStatus.PASS, "Category allowed")
if mandate.require_user_approval:
    return RuleResult("P06", RuleStatus.REQUIRE_APPROVAL, "Unlisted category; requires one-time exception")
return RuleResult("P06", RuleStatus.BLOCK, "Category not in allowlist")
```

### P07 — Quantity Restrictions
```python
if proposal.quantity < 1:
    return RuleResult("P07", RuleStatus.BLOCK, "Invalid quantity")
exceeds_item = proposal.quantity > mandate.max_quantity_per_item
exceeds_count = proposal.quantity > mandate.max_item_count
if not exceeds_item and not exceeds_count:
    return RuleResult("P07", RuleStatus.PASS, "Quantity within limits")
if mandate.require_user_approval:
    return RuleResult("P07", RuleStatus.REQUIRE_APPROVAL, "Quantity exceeds limit; requires approval")
return RuleResult("P07", RuleStatus.BLOCK, "Quantity exceeds limit")
```

### P08 — Currency Consistency
```python
if proposal.currency != mandate.currency:
    return RuleResult("P08", RuleStatus.BLOCK, f"Currency mismatch: {proposal.currency} != {mandate.currency}")
return RuleResult("P08", RuleStatus.PASS, "Currency matches mandate")
```

### P09 — Live Price Validation & Tolerance
```python
oracle = context.oracle_result
if oracle is None or not oracle.is_available:
    return RuleResult("P09", RuleStatus.NOT_APPLICABLE, "Live price oracle unavailable")
if oracle.is_stale:
    return RuleResult("P09", RuleStatus.NOT_APPLICABLE, "Live price oracle data stale")
if oracle.live_price is None:
    return RuleResult("P09", RuleStatus.NOT_APPLICABLE, "Product not in price oracle")
if oracle.live_price <= Decimal("0.00"):
    return RuleResult("P09", RuleStatus.BLOCK, "Invalid price returned by oracle")
variance = abs(proposal.unit_price - oracle.live_price) / oracle.live_price
if variance <= Decimal("0.05"):
    return RuleResult("P09", RuleStatus.PASS, f"Price variance within tolerance ({variance:.2%})")
if mandate.require_user_approval:
    return RuleResult("P09", RuleStatus.REQUIRE_APPROVAL, f"Price variance {variance:.2%} requires approval")
return RuleResult("P09", RuleStatus.BLOCK, f"Price variance {variance:.2%} exceeds tolerance")
```

### P10 — Daily Spend Reservation & Limit
```python
if context.daily_txn_limit is None:
    return RuleResult("P10", RuleStatus.NOT_APPLICABLE, "No daily transaction limit configured")
projected = context.today_reserved + proposal.total
if projected <= context.daily_txn_limit:
    return RuleResult("P10", RuleStatus.PASS, "Daily reservation within limit")
if mandate.require_user_approval:
    return RuleResult("P10", RuleStatus.REQUIRE_APPROVAL, "Daily limit exceeded; requires approval")
return RuleResult("P10", RuleStatus.BLOCK, "Daily transaction limit exceeded")
```

### P11 — Duplicate Purchase & Replay Protection
```python
if not proposal.fingerprint:
    return RuleResult("P11", RuleStatus.BLOCK, "Missing commercial fingerprint")
existing = db_lookup_fingerprint(proposal.fingerprint)
if existing:
    if existing.proposal_id == proposal.proposal_id:
        return RuleResult("P11", RuleStatus.PASS, "Idempotent replay of existing proposal")
    return RuleResult("P11", RuleStatus.BLOCK, "Duplicate commercial terms across proposals")
if proposal.idempotency_key:
    existing_key = db_lookup_idempotency_key(proposal.idempotency_key)
    if existing_key and existing_key.fingerprint != proposal.fingerprint:
        return RuleResult("P11", RuleStatus.BLOCK, "Idempotency key reused with different terms")
return RuleResult("P11", RuleStatus.PASS, "Unique proposal")
```

### P12 — Product Data Integrity & SSRF/Injection Defense
```python
# P12-A: Content hash verification
if not hmac.compare_digest(proposal_content_hash(proposal.model_dump()), proposal.content_hash):
    return RuleResult("P12", RuleStatus.BLOCK, "Proposal content hash mismatch (tampered)")
# P12-B: Disallowed control characters & SSRF checks
for text in [proposal.product_name, proposal.product_id, proposal.product_source]:
    if CONTROL_CHAR_PATTERN.search(text):
        return RuleResult("P12", RuleStatus.BLOCK, "Disallowed control characters detected")
if not proposal.product_source.startswith(("https://", "http://")):
    return RuleResult("P12", RuleStatus.BLOCK, "Invalid product_source URI scheme")
if "@" in parse_url(proposal.product_source).netloc:
    return RuleResult("P12", RuleStatus.BLOCK, "product_source contains credentials")
if is_private_or_loopback_host(proposal.product_source):
    return RuleResult("P12", RuleStatus.BLOCK, "product_source targets private/loopback host (SSRF)")
# P12-C: Prompt-injection heuristic classification
for text in [proposal.product_name, proposal.product_id]:
    if contains_injection_pattern(text):
        return RuleResult("P12", RuleStatus.BLOCK, "Potential prompt injection indicator detected")
return RuleResult("P12", RuleStatus.PASS, "Integrity, URL syntax, and content checks passed")
```

---

*End of POLICY_RULES.md v2.1*
