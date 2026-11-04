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
