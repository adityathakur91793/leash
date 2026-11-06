# LEASH Security Model

## Core Principle
**AI proposes. Deterministic code authorizes. The user approves. PayPal executes.**

No AI purchasing agent can execute a purchase without an explicit, cryptographically verifiable human mandate.

---

## Mandate Security Architecture (Phase 2A)

A mandate represents explicit purchasing permissions granted by a user to an AI shopping agent (e.g., *"Buy a birthday gift for my mom, under \$40, from approved stores only, no subscriptions, valid for 24 hours"*).

### 1. Deterministic Canonical Serialization
To guarantee that cryptographic signatures are reproducible and tamper-evident:
- All dictionary keys are sorted alphabetically (`sort_keys=True`).
- Whitespace is eliminated with compact separators (`separators=(',', ':')`).
- `Decimal` monetary figures are serialized as fixed 2-decimal-place strings (e.g. `"40.00"`). Floating-point representation is forbidden.
- Datetimes are normalized to timezone-aware UTC ISO 8601 strings.
- Lists (`allowed_merchants`, `allowed_categories`) are sanitized, lowercased, deduplicated, and sorted in lexicographical order.
- The signed payload encompasses all permission boundaries: budget, currency, merchants, categories, item caps, quantity limits, per-item limits, subscription flags, approval requirements, creation timestamp, and expiration.

### 2. Cryptographic Signing & Verification
- **Algorithm:** HMAC-SHA256.
- **Key Management:** The signing key is loaded strictly from environment configuration (`MANDATE_SIGNING_KEY`). There is no hardcoded application default. Minimum key length is enforced at 32 bytes (256 bits) when settings are loaded.
- **Constant-Time Verification:** Signatures are compared using `hmac.compare_digest` to prevent timing attacks.
- **Security Properties:**
  - *Integrity:* Any alteration to budget, allowed merchants, categories, or validity windows invalidates the signature immediately.
  - *Authenticity:* Confirms the mandate was issued by the trusted backend.
  - *Limitation:* HMAC provides symmetric authentication within the server/backend trust boundary. It does not provide asymmetric public non-repudiation (which can be introduced in later phases via ECDSA/Ed25519 if external counter-parties require public verification).

### 3. Lifecycle Safeguards & Immutability
- **Strict State Progression:** `DRAFT` → `CONFIRMED` → `SIGNED` → `ACTIVE`.
- **Pre-Signing Confirmation:** A `DRAFT` mandate cannot be signed until the user explicitly confirms it (`POST /confirm`).
- **Terminal States:** `REVOKED` and `EXPIRED` states are permanent and terminal. An expired or revoked mandate can never be activated or verified as valid.
- **Immutability:** Once a mandate is confirmed or signed, its permissions are locked. SQLite database updates explicitly exclude permission columns during conflict resolution. Any change in permissions requires creating a new mandate.

---

## Purchase Proposal Security (Phase 2B)

A proposal is an agent-submitted purchase *request*. It is not a mandate, not an
authorization decision, and not a payment instruction.

### 1. Strict Input Validation
- Pydantic `extra="forbid"` rejects unexpected fields, including client totals,
  authorization flags, and payment tokens.
- All money uses `Decimal` with at most two fractional digits. Unit price must
  be strictly positive. Tax, shipping, and other charges may be zero but never
  negative.
- Quantity is an integer in `[1, 10000]`.
- Currency must be a supported uppercase ISO 4217 code and must match the
  associated mandate currency.

### 2. Server-Side Totals
Subtotal (`quantity * unit_price`) and total
(`subtotal + tax + shipping + other_charges`) are always recalculated by the
server. Client-calculated totals are not accepted on create.

### 3. Untrusted Product Content
Product names, identifiers, categories, and source/reference strings are treated
as opaque untrusted data:
- Control characters are rejected.
- Values are stored and returned as literal text.
- LEASH never executes, evaluates, or fetches product content as instructions
  or code.

### 4. Mandate Binding and Merchant Identity
- Each proposal references exactly one existing `mandate_id` (foreign key).
- Merchant identifiers are canonicalized with the same domain rules as mandates.

### 5. Immutability, Integrity, and Duplicates
- Commercial terms are insert-only. Status transitions never rewrite prices,
  quantities, or merchants. Changed terms require a **new** proposal.
- A SHA-256 `content_hash` of canonical proposal JSON is verified on read.
- Duplicate protection uses a stable commercial-term `fingerprint` plus an
  optional `idempotency_key`. Replaying the same request returns the original
  proposal. Reusing an idempotency key with different terms is rejected.

### 6. Separation from Authorization and Payment
Proposal create/get/list do not sign mandates, do not evaluate policy, and do
not call PayPal. An `APPROVED` status (later phases) remains a recorded
evaluation outcome, not a payment execution.

---

## Threat Model & Mitigations

| Threat Vector | Mitigation Strategy |
|---|---|
| **Agent Prompt Injection** | The shopping agent has zero access to payment credentials and zero ability to modify mandate permissions. The backend verifies cryptographic integrity deterministically. |
| **Agent Exceeds Spending Limit** | Strict `Decimal` budget checks and mandatory `max_total_budget` validation. |
| **Unauthorized Store Purchases** | Explicit whitelist of merchant domains (`allowed_merchants`). |
| **Silent Subscription Traps** | `allow_subscriptions` defaults to `False`. |
| **Replay / Zombie Mandates** | Mandatory `expires_at` UTC timestamp checked on every verification. |
| **Signature Forgery / Key Tampering** | Server rejects weak keys (< 32 bytes). Signature comparison uses constant-time checks. Raw keys are never stored in SQLite or exposed to frontend code. |
| **In-Flight Data Tampering** | Any payload modification invalidates the HMAC signature. |
| **Agent-supplied fake totals** | Server recalculates subtotal and total; extra total fields are forbidden. |
| **Prompt-like product copy** | Product fields are opaque text with control characters stripped/rejected; never executed. |
| **Proposal replay / duplicate spend request** | Fingerprint uniqueness and optional idempotency keys return the original proposal. |
| **In-place proposal mutation** | Commercial columns are write-once; content hash verification detects storage tampering. |
| **Proposal without mandate** | Foreign key plus explicit existence check; missing mandate returns 404. |

---

## Shopping Agent Boundary (Phase 4)

- Gemini receives a fixed system instruction and only three declared functions;
  the backend independently checks tool names and validates every argument with
  strict Pydantic schemas. Tool rounds, message length, response bytes, and raw
  HTTP request bytes are bounded.
- The API identity comes exclusively from the validated Bearer session. The
  proposal path reloads the mandate, verifies `owner_user_id`, signature,
  ACTIVE state, and expiry, and fails closed when the emergency kill switch is
  active.
- `propose_purchase` accepts only a catalogue ID and quantity from the model.
  Merchant, product name, price, currency, and availability are resolved from
  the local catalogue. The existing proposal service creates the proposal and
  the existing deterministic policy service evaluates it.
- The agent does not import or call approval-token, PayPal order, or capture
  services. An agent result is not a user approval or payment result.
- Catalogue data is clearly simulated. HTML is removed, script/style contents
  are omitted, descriptions are length-capped, and suspicious instruction-like
  text is quarantined. Indicator matching is heuristic defense in depth; it
  cannot guarantee detection of prompt injection.
- Gemini credentials are read only from backend environment configuration,
  transmitted in the provider API key header, and excluded from prompts,
  frontend configuration, and application logs. The React app sends only its
  session Bearer token using `VITE_API_URL`.
