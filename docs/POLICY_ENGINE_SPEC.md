# LEASH — Deterministic Policy Engine Specification (Phase 2C)

**Document:** `docs/POLICY_ENGINE_SPEC.md`
**Revision:** v2.1 (Security Review V2 Approved)
**Date:** 2026-11-04

> [!IMPORTANT]
> This is the authoritative specification for Phase 2C. Implementation must not begin until this specification receives explicit review approval.

---

## 1. Executive Summary & Security Principles

The **Policy Engine** is a deterministic, side-effect-free authorization component in Python that evaluates a purchase proposal against an active, signed user mandate.

### Core Architecture Axiom
$$\text{AI Proposes} \longrightarrow \text{Deterministic Code Authorizes} \longrightarrow \text{User Approves} \longrightarrow \text{PayPal Executes}$$

1. **Zero LLM / Zero External Network in Authorization:** The policy engine executes 100% deterministic local Python logic. It never invokes an LLM, never performs external network calls, never fetches URLs, and never initiates payment.
2. **Fail-Closed Default:** Any missing signature, expired mandate, malformed parameter, currency mismatch, or duplicate replay immediately yields `BLOCK`. Missing evidence is never treated as a pass.
3. **Immutability of Signed Mandates:** User approval of an individual purchase proposal **never** mutates, expands, or relaxes the signed mandate. The mandate's `max_total_budget`, `allowed_merchants`, and `allowed_categories` remain strictly unchanged.
4. **Hard Blocks vs. Human Exceptions:** Hard security violations (tampering, budget overruns, invalid credentials, duplicate transactions) are unconditionally unoverridable. One-time exceptions (e.g., an unlisted merchant or category under an approval-enabled mandate) require an explicit, cryptographic one-time authorization bound solely to that exact proposal.
5. **Exact Arithmetic:** All monetary computations use `Decimal` with fixed quantum `0.01` (`ROUND_HALF_UP`). Floats are strictly prohibited.
6. **Timezone Integrity:** All timestamps are timezone-aware UTC (`datetime.timezone.utc`).

---

## 2. Component Architecture & Data Flow

```
                                 HTTP Request
                                      │
                                      ▼
                        ┌───────────────────────────┐
                        │ FastAPI Endpoint          │
                        │ /api/v1/proposals         │
                        └─────────────┬─────────────┘
                                      │
                                      ▼
                        ┌───────────────────────────┐
                        │ SQLite Storage Layer      │
                        │ (BEGIN IMMEDIATE)         │
                        │ - Lock DB connection      │
                        │ - Fetch Proposal & Mandate│
                        │ - Aggregate Today Spend   │
                        └─────────────┬─────────────┘
                                      │
                    Proposal, Mandate,│ EvaluationContext
                                      ▼
                        ┌───────────────────────────┐
                        │ Policy Engine (Pure Func) │
                        │ evaluate(proposal,        │
                        │          mandate,         │
                        │          context)         │
                        │                           │
                        │ Evaluates Rules P01 - P12 │
                        └─────────────┬─────────────┘
                                      │
                         PolicyDecision (Typed)
                                      ▼
                        ┌───────────────────────────┐
                        │ Persistence & Reservation │
                        │ - If ALLOW / REQ_APPROVAL:│
                        │   Reserve Daily Spend     │
                        │ - Store Policy Evaluation │
                        │ - COMMIT                  │
                        └─────────────┬─────────────┘
                                      │
                                      ▼
                                HTTP Response
```

### Component Boundaries
- **Policy Engine:** Pure mathematical evaluation. Accepts `(proposal: Proposal, mandate: Mandate, context: EvaluationContext)` and returns `PolicyDecision`. It has zero database connections, zero network sockets, and zero mutation side-effects.
- **Evaluation Service:** Manages transactional state in SQLite, acquires write locks (`BEGIN IMMEDIATE`), gathers spend aggregations, invokes the policy engine, records spend reservations, and persists audit logs.
- **API Layer:** Handles request validation, HTTP serialization, and status code mapping.

---

## 3. Data Contracts & Interfaces

### 3.1 EvaluationContext
```python
@dataclass(frozen=True)
class EvaluationContext:
    """Read-only context injected into the policy engine."""
    now: datetime
    today_reserved: Decimal
    daily_txn_limit: Decimal | None
    cumulative_mandate_spent: Decimal
    oracle_result: OracleResult | None
```

- `now`: Authoritative UTC timestamp passed from service layer (ensures test determinism).
- `today_reserved`: Total reserved spend for this mandate on current UTC calendar day (`SUBMITTED`, `EVALUATED`, and `APPROVED` proposals).
- `daily_txn_limit`: Optional daily cap from mandate or user profile (if None, P10 is `NOT_APPLICABLE`).
- `cumulative_mandate_spent`: Total lifetime spend approved or captured against this mandate ID.
- `oracle_result`: Optional read-only price verification feed for P09.

### 3.2 OracleResult (for Rule P09)
```python
@dataclass(frozen=True)
class OracleResult:
    """External live-price reading provided to the engine."""
    product_id: str
    live_price: Decimal | None
    fetched_at: datetime
    is_available: bool
    is_stale: bool
```

### 3.3 RuleStatus & RuleResult
```python
class RuleStatus(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"

@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    rule_name: str
    status: RuleStatus
    reason: str
    evidence: dict[str, Any]
    evaluated_at: datetime
```

### 3.4 PolicyDecision
```python
class DecisionOutcome(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"

@dataclass(frozen=True)
class PolicyDecision:
    proposal_id: str
    mandate_id: str
    decision: DecisionOutcome
    decision_at: datetime
    rules: list[RuleResult]
    blocking_rules: list[str]
    approval_required_rules: list[str]
    is_one_time_exception: bool
    exception_types: list[str]
```

---

## 4. Decision Model & Strict Precedence

Every evaluation runs **all 12 rules**. The engine never short-circuits on the first failure, guaranteeing a complete and auditable security evaluation log.

### Evaluation Precedence Order

$$\text{Decision} =
\begin{cases}
\mathbf{BLOCK}, & \text{if } \exists r \in \text{Rules} \text{ where } r.\text{status} = \mathbf{BLOCK} \\
\mathbf{REQUIRE\_APPROVAL}, & \text{else if } \exists r \text{ where } r.\text{status} = \mathbf{REQUIRE\_APPROVAL} \lor \text{mandate.require\_user\_approval} = \mathbf{True} \\
\mathbf{ALLOW}, & \text{otherwise}
\end{cases}$$

1. **Unconditional BLOCK:** If any rule yields `BLOCK`, the final outcome is `BLOCK`. No user approval, admin override, or AI audit can overturn a `BLOCK`.
2. **REQUIRE_APPROVAL:** If there are no `BLOCK` findings, but:
   - One or more rules produced `REQUIRE_APPROVAL` (e.g. valid one-time merchant or category exception, or price variance), OR
   - The mandate has `require_user_approval == True` (standard user sign-off required for purchases);
   Then the outcome is `REQUIRE_APPROVAL`.
3. **ALLOW:** Produced **only** when all rules evaluate to `PASS` (or `NOT_APPLICABLE`) AND `mandate.require_user_approval == False`.

### Cryptographic Approval Binding
When a proposal requires approval (`REQUIRE_APPROVAL`):
1. The backend issues a single-use, time-bound approval token:
   $$\text{ApprovalToken} = \text{HMAC-SHA256}\Big(K_{\text{approval}}, \text{proposal\_id} \mathbin{\Vert} \text{content\_hash} \mathbin{\Vert} \text{user\_id} \mathbin{\Vert} \text{expires\_at}\Big)$$
2. The user is presented with the immutable commercial terms (`merchant`, `product_name`, `quantity`, `unit_price`, `total`, `currency`) taken directly from the database record, **never** from user client input.
3. User approval authorizes **only this specific proposal instance**. It is consumed upon payment execution and expires after `APPROVAL_TOKEN_TTL` (default 15 minutes).
4. **Mandate Non-Mutation:** Approval of an unlisted merchant or category creates a one-time authorization record linked to `proposal_id`. It **never** appends entries to `mandate.allowed_merchants` or `mandate.allowed_categories`.

---

## 5. Detailed Specification of Policy Rules (P01 – P12)

### P01 — Mandate Signature Validity
- **Purpose:** Cryptographically verify mandate authenticity and detect tampering.
- **Required Inputs:** `mandate.signature`, `mandate.signed_at`, `mandate.canonical_payload()`, `context.now`.
- **Signing Secret Source:** Loaded strictly from `settings.MANDATE_SIGNING_KEY` (minimum 32 bytes).
- **Algorithm:**
  1. If `mandate.signature` is missing/empty $\rightarrow$ `BLOCK` (`P01_MISSING_SIGNATURE`).
  2. If `mandate.signed_at` is missing $\rightarrow$ `BLOCK` (`P01_MISSING_SIGNED_AT`).
  3. If `mandate.signed_at > context.now` $\rightarrow$ `BLOCK` (`P01_FUTURE_SIGNED_AT`).
  4. Compute $E = \text{sign\_payload}(\text{mandate.canonical\_payload()}, \text{settings.MANDATE\_SIGNING\_KEY})$.
  5. If not `hmac.compare_digest(E, mandate.signature)` $\rightarrow$ `BLOCK` (`P01_SIGNATURE_MISMATCH`).
  6. Return `PASS`.
- **Pass Condition:** Signature matches canonical payload and `signed_at <= now`.
- **Hard-Block Condition:** Missing signature, invalid format, timestamp in future, or signature mismatch.
- **Can User Approval Override?** ❌ **No. Hard security boundary.**
- **Missing Data Behavior:** Any missing signature field results in `BLOCK`.
- **Error Code:** `P01_INVALID_SIGNATURE`.

---

### P02 — Mandate Expiration & Active Lifecycle Status
- **Purpose:** Prevent execution against expired, draft, revoked, or stale mandates.
- **Required Inputs:** `mandate.status`, `mandate.expires_at`, `context.now`.
- **Algorithm:**
  1. If `mandate.status != MandateStatus.ACTIVE` $\rightarrow$ `BLOCK` (`P02_MANDATE_NOT_ACTIVE`).
  2. If `mandate.expires_at <= context.now` $\rightarrow$ `BLOCK` (`P02_MANDATE_EXPIRED`).
  3. Return `PASS`.
- **Pass Condition:** `status == ACTIVE` and `expires_at > now`.
- **Hard-Block Condition:** Status is `DRAFT`, `CONFIRMED`, `SIGNED`, `EXPIRED`, or `REVOKED`; or `expires_at <= now`.
- **Can User Approval Override?** ❌ **No. Expired or inactive mandates cannot authorize transactions.**
- **Missing Data Behavior:** Missing status or expiration results in `BLOCK`.
- **Error Code:** `P02_EXPIRED_OR_INACTIVE`.

---

### P03 — Total Amount Within Mandate Budget Ceiling
- **Purpose:** Enforce immutable ceiling of mandate budget. User approval cannot expand signed mandate limits.
- **Required Inputs:** `mandate.max_total_budget`, `proposal.total`, `context.cumulative_mandate_spent`.
- **Algorithm:**
  1. Validate `proposal.total > 0` and `mandate.max_total_budget > 0`.
  2. Check single-proposal ceiling: If `proposal.total > mandate.max_total_budget` $\rightarrow$ `BLOCK` (`P03_PROPOSAL_EXCEEDS_BUDGET_CEILING`).
  3. Check cumulative mandate ceiling: If `(context.cumulative_mandate_spent + proposal.total) > mandate.max_total_budget` $\rightarrow$ `BLOCK` (`P03_CUMULATIVE_BUDGET_EXCEEDED`).
  4. Return `PASS`.
- **Pass Condition:** Proposal total and cumulative spend do not exceed `mandate.max_total_budget`.
- **Hard-Block Condition:** Any excess over `mandate.max_total_budget`.
- **Can User Approval Override?** ❌ **No. User approval must NEVER expand a signed mandate budget.** To increase budget, a new mandate must be confirmed, signed, and activated.
- **Missing Data Behavior:** Missing budget or proposal total yields `BLOCK`.
- **Error Code:** `P03_BUDGET_EXCEEDED`.

---

### P04 — Per-Item Spending Limit
- **Purpose:** Enforce optional single-unit price ceiling defined in mandate.
- **Required Inputs:** `mandate.max_price_per_item`, `proposal.unit_price`.
- **Algorithm:**
  1. If `mandate.max_price_per_item is None` $\rightarrow$ Return `NOT_APPLICABLE`.
  2. If `proposal.unit_price <= mandate.max_price_per_item` $\rightarrow$ Return `PASS`.
  3. If `proposal.unit_price > mandate.max_price_per_item`:
     - If `mandate.require_user_approval == True` $\rightarrow$ Return `REQUIRE_APPROVAL` (`P04_ITEM_PRICE_APPROVAL_REQUIRED`).
     - Else $\rightarrow$ Return `BLOCK` (`P04_ITEM_PRICE_EXCEEDED`).
- **Pass Condition:** Unit price $\le$ `max_price_per_item`.
- **Hard-Block Condition:** Unit price exceeds limit when `require_user_approval == False`.
- **Can User Approval Override?** ⚠️ **Yes (One-time exception)**, but only if mandate has approval enabled AND proposal total remains within P03 mandate budget.
- **Missing Data Behavior:** If `max_price_per_item` is omitted from mandate, rule is `NOT_APPLICABLE`. If `proposal.unit_price` is missing, `BLOCK`.
- **Error Code:** `P04_ITEM_LIMIT_VIOLATION`.

---

### P05 — Merchant Allowlist Validation
- **Purpose:** Validate merchant identity against mandate allowlist using canonical, IDNA-safe exact matching.
- **Required Inputs:** `mandate.allowed_merchants`, `proposal.merchant`.
- **Merchant Normalization & Validation Algorithm:**
  1. Input `m` must be string. Reject if contains URI scheme (`://`), path (`/`), port (`:`), credentials (`@`), or whitespace.
  2. Convert to lowercase.
  3. Apply IDNA encoding: `m_ascii = idna.encode(m).decode("ascii")`. If encoding fails $\rightarrow$ `BLOCK` (`P05_INVALID_MERCHANT_IDNA`).
  4. Validate regex: `MERCHANT_IDENTIFIER_REGEX.match(m_ascii)`.
  5. Exact match: Check if `m_ascii` is in `mandate.allowed_merchants`.
     - Subdomain wildcarding is prohibited (`example.com` does NOT match `store.example.com`).
     - TLD suffix matching is prohibited (`amazon.com` does NOT match `amazon.co.uk`).
  6. If exact match found $\rightarrow$ Return `PASS`.
  7. If not found in allowlist:
     - If `mandate.require_user_approval == True` $\rightarrow$ Return `REQUIRE_APPROVAL` (`P05_MERCHANT_EXCEPTION_REQUEST`).
     - Else $\rightarrow$ Return `BLOCK` (`P05_MERCHANT_NOT_ALLOWED`).
- **Pass Condition:** Normalized merchant exactly exists in `allowed_merchants`.
- **Hard-Block Condition:** Malformed merchant identifier, or unlisted merchant when approval disabled.
- **Can User Approval Override?** ⚠️ **Yes (One-time exception)**. Generates a one-time authorization token for this proposal only. Does NOT mutate mandate's `allowed_merchants`.
- **Missing Data Behavior:** Empty allowlist or missing merchant results in `BLOCK`.
- **Error Code:** `P05_MERCHANT_NOT_ALLOWED`.

---

### P06 — Product Category Allowlist Validation
- **Purpose:** Restrict purchasing to permitted category classifications.
- **Required Inputs:** `mandate.allowed_categories`, `proposal.category`.
- **Algorithm:**
  1. If `mandate.allowed_categories` is empty (`[]`) or `None` $\rightarrow$ `BLOCK` (`P06_NO_CATEGORIES_PERMITTED`). Empty allowlist represents zero permitted categories.
  2. Normalize category: `c = proposal.category.strip().lower()`.
  3. If `c` exists in `mandate.allowed_categories` $\rightarrow$ Return `PASS`.
  4. If `c` not in allowlist:
     - If `mandate.require_user_approval == True` $\rightarrow$ Return `REQUIRE_APPROVAL` (`P06_CATEGORY_EXCEPTION_REQUEST`).
     - Else $\rightarrow$ Return `BLOCK` (`P06_CATEGORY_NOT_ALLOWED`).
- **Pass Condition:** Normalized category is present in allowlist.
- **Hard-Block Condition:** Unlisted category when approval disabled; or empty allowlist.
- **Can User Approval Override?** ⚠️ **Yes (One-time exception)**. Bound to proposal instance only.
- **Missing Data Behavior:** Empty allowlist is strictly closed (blocks all). Missing proposal category yields `BLOCK`.
- **Error Code:** `P06_CATEGORY_NOT_ALLOWED`.

---

### P07 — Quantity Restrictions
- **Purpose:** Enforce quantity limits (`max_quantity_per_item` and `max_item_count`).
- **Required Inputs:** `mandate.max_quantity_per_item`, `mandate.max_item_count`, `proposal.quantity`.
- **Algorithm:**
  1. If `proposal.quantity < 1` $\rightarrow$ `BLOCK` (`P07_INVALID_QUANTITY`).
  2. Check per-item limit: If `proposal.quantity > mandate.max_quantity_per_item`:
     - If `mandate.require_user_approval == True` $\rightarrow$ Return `REQUIRE_APPROVAL` (`P07_QUANTITY_APPROVAL_REQUIRED`).
     - Else $\rightarrow$ Return `BLOCK` (`P07_QUANTITY_PER_ITEM_EXCEEDED`).
  3. Check item-count limit: If `proposal.quantity > mandate.max_item_count`:
     - If `mandate.require_user_approval == True` $\rightarrow$ Return `REQUIRE_APPROVAL` (`P07_ITEM_COUNT_APPROVAL_REQUIRED`).
     - Else $\rightarrow$ Return `BLOCK` (`P07_MAX_ITEM_COUNT_EXCEEDED`).
  4. Return `PASS`.
- **Pass Condition:** Quantity satisfies both configured bounds.
- **Hard-Block Condition:** Quantity exceeds limits when approval is disabled.
- **Can User Approval Override?** ⚠️ **Yes (One-time exception)**.
- **Missing Data Behavior:** Missing proposal quantity yields `BLOCK`.
- **Error Code:** `P07_QUANTITY_LIMIT_EXCEEDED`.

---

### P08 — Currency Consistency
- **Purpose:** Enforce single-currency invariant across proposal and mandate.
- **Required Inputs:** `mandate.currency`, `proposal.currency`.
- **Algorithm:**
  1. Validate both are supported 3-letter ISO-4217 strings in `SUPPORTED_CURRENCIES`.
  2. If `proposal.currency != mandate.currency` $\rightarrow$ `BLOCK` (`P08_CURRENCY_MISMATCH`).
  3. Return `PASS`.
- **Pass Condition:** Strict string equality of valid currency codes.
- **Hard-Block Condition:** Any currency mismatch.
- **Can User Approval Override?** ❌ **No. Hard security boundary.**
- **Missing Data Behavior:** Missing currency yields `BLOCK`.
- **Error Code:** `P08_CURRENCY_MISMATCH`.

---

### P09 — Live Price Validation & Tolerance
- **Purpose:** Protect against merchant price manipulation, stale quotes, or flash price spikes.
- **Required Inputs:** `proposal.unit_price`, `proposal.product_id`, `context.oracle_result`.
- **Configured Constant:** `PRICE_TOLERANCE = Decimal("0.05")` (5.00%).
- **Staleness Threshold:** `ORACLE_STALENESS_THRESHOLD = timedelta(minutes=5)`.
- **Algorithm:**
  1. If `context.oracle_result is None` $\rightarrow$ Return `NOT_APPLICABLE` (`P09_NO_ORACLE_CONFIGURED`).
  2. If `not context.oracle_result.is_available` $\rightarrow$ Return `NOT_APPLICABLE` (`P09_ORACLE_UNAVAILABLE`).
  3. If `context.oracle_result.is_stale` $\rightarrow$ Return `NOT_APPLICABLE` (`P09_ORACLE_DATA_STALE`).
  4. If `context.oracle_result.live_price is None` $\rightarrow$ Return `NOT_APPLICABLE` (`P09_PRODUCT_NOT_IN_ORACLE`).
  5. If `context.oracle_result.live_price <= Decimal("0.00")` $\rightarrow$ `BLOCK` (`P09_INVALID_ORACLE_PRICE`).
  6. Calculate price delta:
     $$\Delta_{\text{rel}} = \frac{|\text{proposal.unit\_price} - \text{oracle.live\_price}|}{\text{oracle.live\_price}}$$
  7. If $\Delta_{\text{rel}} \le \text{PRICE\_TOLERANCE}$ $\rightarrow$ Return `PASS`.
  8. If $\Delta_{\text{rel}} > \text{PRICE\_TOLERANCE}$:
     - If `mandate.require_user_approval == True` $\rightarrow$ Return `REQUIRE_APPROVAL` (`P09_PRICE_VARIANCE_REQUIRES_APPROVAL`).
     - Else $\rightarrow$ Return `BLOCK` (`P09_PRICE_TOLERANCE_EXCEEDED`).
- **Pass Condition:** Oracle is fresh and price deviation $\le 5\%$.
- **Hard-Block Condition:** Negative/zero oracle price; or variance $> 5\%$ with approval disabled.
- **NOT_APPLICABLE Condition:** Oracle unavailable, stale, or product not indexed. (Never reports false PASS).
- **Checkout Revalidation:** Before final PayPal capture in Phase 3, this check is rerun. If price variance now exceeds tolerance, authorization is revoked.
- **Can User Approval Override?** ⚠️ **Yes (User confirms price difference)**, provided fresh oracle data exists.
- **Missing Data Behavior:** Missing oracle data results in `NOT_APPLICABLE` with explicit audit evidence.
- **Error Code:** `P09_PRICE_TOLERANCE_EXCEEDED`.

---

### P10 — Daily Spend Reservation & Limit
- **Purpose:** Prevent velocity-based balance depletion and concurrent race condition budget overruns.
- **Required Inputs:** `mandate.daily_txn_limit` (or context default), `proposal.total`, `context.today_reserved`.
- **Reservation Accounting Lifecycle:**

| Proposal Lifecycle State | Counted Against Daily Spend? | Rationale |
|---|:---:|---|
| `SUBMITTED` | ✅ Yes | Pending evaluation; holds a provisional reservation |
| `EVALUATED` | ✅ Yes | Evaluation complete; awaiting execution or approval |
| `APPROVED` | ✅ Yes | User-approved commitment; pending capture |
| `CAPTURED` | ✅ Yes | Final settled expenditure |
| `REJECTED` | ❌ No | Released immediately from reservation |
| `WITHDRAWN` | ❌ No | Released immediately from reservation |
| `EXPIRED` | ❌ No | Released upon TTL expiry of approval token |

- **Algorithm:**
  1. If `context.daily_txn_limit is None` $\rightarrow$ Return `NOT_APPLICABLE` (`P10_NO_DAILY_LIMIT_CONFIGURED`).
  2. Compute projected spend:
     $$\text{Projected} = \text{context.today\_reserved} + \text{proposal.total}$$
  3. If $\text{Projected} \le \text{context.daily\_txn\_limit}$ $\rightarrow$ Return `PASS`.
  4. If $\text{Projected} > \text{context.daily\_txn\_limit}$:
     - If `mandate.require_user_approval == True` $\rightarrow$ Return `REQUIRE_APPROVAL` (`P10_DAILY_PACE_EXCEEDED`).
     - Else $\rightarrow$ Return `BLOCK` (`P10_DAILY_LIMIT_EXCEEDED`).
- **Concurrency & Race Condition Mitigation:**
  Executed inside a SQLite `BEGIN IMMEDIATE` transaction. Read of `today_reserved` and write of proposal record occur under the same reservation lock.
- **Pass Condition:** Projected total daily reservation $\le$ daily limit.
- **Hard-Block Condition:** Projected spend exceeds daily limit when approval is disabled.
- **Can User Approval Override?** ⚠️ **Yes (User confirms daily pace overrun)**, provided proposal total is still within mandate budget P03.
- **Missing Data Behavior:** Missing daily limit config yields `NOT_APPLICABLE`.
- **Error Code:** `P10_DAILY_LIMIT_EXCEEDED`.

---

### P11 — Duplicate Purchase & Replay Protection
- **Purpose:** Guarantee strict idempotency and block replay attacks across concurrent threads.
- **Required Inputs:** `proposal.fingerprint`, `proposal.idempotency_key`, database index lookups.
- **Algorithm:**
  1. If `proposal.fingerprint` is missing or empty $\rightarrow$ `BLOCK` (`P11_MISSING_FINGERPRINT`).
  2. Query database for existing proposals matching `proposal.fingerprint`:
     - If found AND `existing.proposal_id == proposal.proposal_id`: Return `PASS` (Identical replay: return existing record without new charges or reservations).
     - If found AND `existing.proposal_id != proposal.proposal_id`: Return `BLOCK` (`P11_DUPLICATE_PROPOSAL_DETECTED`).
  3. If `proposal.idempotency_key` is provided:
     - Query database for existing proposal matching `proposal.idempotency_key`:
     - If found AND `existing.fingerprint == proposal.fingerprint`: Return `PASS` (Idempotent replay).
     - If found AND `existing.fingerprint != proposal.fingerprint`: Return `BLOCK` (`P11_IDEMPOTENCY_KEY_CONFLICT`).
  4. If no duplicates found $\rightarrow$ Return `PASS`.
- **Pass Condition:** Fresh proposal or exact idempotent retry.
- **Hard-Block Condition:** Commercial fingerprint collision across different proposals; or idempotency key reused with altered terms.
- **Can User Approval Override?** ❌ **No. Replay protection is an immutable security invariant.**
- **Missing Data Behavior:** Missing fingerprint yields `BLOCK`.
- **Error Code:** `P11_DUPLICATE_PROPOSAL`.

---

### P12 — Product Data Integrity, Safe Content Handling & Injection Classification
- **Purpose:** Enforce data integrity, sanitize untrusted text, prevent SSRF vectors, and classify prompt-injection risks.
- **Sub-Check P12-A: Cryptographic Content Hash Integrity**
  1. Recompute $H = \text{proposal\_content\_hash}(\text{proposal.model\_dump()})$.
  2. If not `hmac.compare_digest(H, proposal.content_hash)` $\rightarrow$ `BLOCK` (`P12_CONTENT_HASH_TAMPERED`).
- **Sub-Check P12-B: Untrusted Content Sanitization & SSRF Defense**
  1. Verify `product_name`, `product_id`, and `product_source` contain no control characters via `CONTROL_CHAR_PATTERN`. If found $\rightarrow$ `BLOCK` (`P12_DISALLOWED_CONTROL_CHAR`).
  2. Validate `product_source` URL structure:
     - Must start strictly with `https://` (or `http://` in local dev). If not $\rightarrow$ `BLOCK` (`P12_INVALID_URL_SCHEME`).
     - Must not contain user credentials (`@` in host) $\rightarrow$ `BLOCK` (`P12_URL_CONTAINS_CREDENTIALS`).
     - Reject private/loopback/cloud-metadata network hosts (SSRF defense):
       - `localhost`, `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`, `::1`, `0.0.0.0`.
       - If matched $\rightarrow$ `BLOCK` (`P12_SSRF_PROHIBITED_HOST`).
  3. `product_source` is **never fetched or dereferenced** by the server.
- **Sub-Check P12-C: Prompt-Injection Heuristic Classification**
  1. Scan `product_name` and `product_id` against high-risk instruction injection patterns:
     - `ignore previous`, `system:`, `[inst]`, `<|im_start|>`, `<script`, `javascript:`, SQL markers (`--`, `/*`, `*/`).
  2. If found $\rightarrow$ `BLOCK` (`P12_PROMPT_INJECTION_INDICATOR`).
  3. *Security Disclaimer:* Heuristic string scanning is a defense-in-depth indicator, not a formal guarantee of safety. Architectural safety is guaranteed because LEASH backend code treats all product text as opaque data and never executes or feeds it into LLM contexts.
- **Pass Condition:** Hash verifies, no control chars, safe URL syntax, no injection indicators.
- **Hard-Block Condition:** Hash mismatch, control characters, SSRF vector, or injection indicators.
- **Can User Approval Override?** ❌ **No. Code and data integrity rules cannot be bypassed.**
- **Missing Data Behavior:** Missing fields result in `BLOCK`.
- **Error Code:** `P12_PRODUCT_INTEGRITY_VIOLATION`.

---

## 6. Concurrency, Atomicity & Transactional Architecture

### Race Condition Scenarios & Mitigations

1. **Concurrent Overspend Attack:** Two concurrent agent workers submit proposals for \$30 each against a \$40 mandate.
   - *Mitigation:* The evaluation and spend reservation run inside `BEGIN IMMEDIATE` in SQLite. Worker 1 acquires the reservation lock, calculates cumulative spend (\$0 + \$30 = \$30 $\le$ \$40), reserves \$30, and commits. Worker 2 blocks on lock, reads updated cumulative spend (\$30), calculates \$30 + \$30 = \$60 > \$40, and is immediately blocked by rule P03.
2. **Duplicate Proposal Race:** Two identical requests submit simultaneously.
   - *Mitigation:* The `proposals.fingerprint` column has a SQLite `UNIQUE INDEX`. The second insert raises `aiosqlite.IntegrityError`, caught by the service layer, which reads and returns the existing proposal.
3. **Time-of-Check to Time-of-Use (TOCTOU) Price Drift:** Merchant raises price after proposal evaluation but before PayPal capture.
   - *Mitigation:* During Phase 3 execution, the checkout controller re-evaluates P09 against fresh live oracle data before invoking PayPal capture.

---

## 7. Open Decisions & Scope Boundaries

| ID | Topic | Current Spec Resolution | Implementation Impact |
|---|---|---|---|
| **OD-1** | Empty Category Allowlist | Empty list strictly means 0 categories permitted (`BLOCK`). | Enforced. (If user wants open categories, a sentinel like `["*"]` will be added in Phase 2D). |
| **OD-2** | Daily Limit Mandate Field | Mandate schema does not yet have `daily_txn_limit`. Context accepts `None` $\rightarrow$ P10 returns `NOT_APPLICABLE`. | No DB schema change required for Phase 2C. |
| **OD-3** | Live Price Oracle Provider | P09 returns `NOT_APPLICABLE` when no oracle is injected. | Production oracle integration deferred to Phase 3. Tests use mock `OracleResult`. |
| **OD-4** | Rolling 24h vs. Calendar Day | P10 calculates spend based on UTC calendar day (`context.now.date()`). | Simple, deterministic, and index-friendly. |

---

*End of POLICY_ENGINE_SPEC.md v2.1*
