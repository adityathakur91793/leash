# LEASH API Contracts

Base URL: `http://localhost:8000`

## Phase 7H Authenticated Activity Overview

### GET /api/v1/activity/overview

Requires an active `Authorization: Bearer <session_token>` session. The server
derives the principal from that session and returns only audit events attached
to that principal's mandates, proposals, and PayPal orders/order attempts.

The response is a strict public projection. It includes recent event timestamp,
event/entity category, safe actor label, human-readable summary,
product/mandate display label, and exact amount/currency when applicable. It
does not return raw payloads, owner IDs, entity IDs, hashes, signatures,
idempotency keys, provider order IDs, or source URLs. The result also includes
all mandate and proposal lifecycle counts, current payment status counts, and
exact confirmed settlement count/totals grouped by currency, plus a distinct
owner-scoped count of unresolved checkout attempts in `UNKNOWN`,
`PROVIDER_CREATED`, or `RECONCILIATION_REQUIRED` state that lack a local PayPal
order row. Attempts remain separate from payment counts and settlements. Global
audit sequence numbers are not part of the public response. Expired mandate
windows are reflected as expired in the overview without mutating stored data.
Only completed local orders with a persisted capture ID count as confirmed
settlements.

`hash_chain_valid` reports cryptographic audit-chain verification only. It does
not establish that the underlying event or business decision was correct.
Activity is read-only; event history remains append-only.

---

## Phase 1 Foundation Endpoints

### GET /health
Health check endpoint.

**Response** `200 OK`
```json
{
  "status": "healthy",
  "timestamp": "2026-11-04T00:00:00.000000+00:00"
}
```

### GET /api/v1/status
System status endpoint.

**Response** `200 OK`
```json
{
  "status": "operational",
  "version": "0.1.0",
  "service": "LEASH"
}
```

---

## Phase 2A Mandate Endpoints

### POST /api/v1/mandates
Create a new purchase authorization mandate in `DRAFT` status.

**Request Body**
```json
{
  "max_total_budget": "40.00",
  "currency": "USD",
  "allowed_merchants": ["amazon.com", "target.com"],
  "allowed_categories": ["gifts", "books"],
  "max_item_count": 2,
  "max_quantity_per_item": 1,
  "max_price_per_item": "25.00",
  "allow_subscriptions": false,
  "require_user_approval": true,
  "expires_at": "2026-11-05T00:00:00Z"
}
```

**Response** `201 Created`
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "schema_version": "1.0",
  "status": "DRAFT",
  "max_total_budget": "40.00",
  "currency": "USD",
  "allowed_merchants": ["amazon.com", "target.com"],
  "allowed_categories": ["books", "gifts"],
  "max_item_count": 2,
  "max_quantity_per_item": 1,
  "max_price_per_item": "25.00",
  "allow_subscriptions": false,
  "require_user_approval": true,
  "created_at": "2026-11-04T00:00:00Z",
  "expires_at": "2026-11-05T00:00:00Z",
  "signature": null,
  "signed_at": null,
  "signature_algorithm": null
}
```

### GET /api/v1/mandates
List recent mandates.

**Query Parameters**
- `limit` (integer, default: 50, min: 1, max: 100)

**Response** `200 OK`
```json
[
  {
    "mandate_id": "man_a1b2c3d4e5f67890",
    "schema_version": "1.0",
    "status": "ACTIVE",
    ...
  }
]
```

### GET /api/v1/mandates/{mandate_id}
Retrieve details of a specific mandate by its ID.

- **Response:** `200 OK` (returns full `Mandate` object)
- **Error:** `404 Not Found` if mandate does not exist.

---

## Phase 4 Shopping Agent

Both endpoints below require `Authorization: Bearer <session_token>`. The raw
session token is issued once by `POST /api/v1/sessions`; the client must keep it
private and must not send a principal or owner ID.

### GET /api/v1/shopping-agent/mandates

Returns only the current session principal's active, unexpired, signature-
verified mandates. Mandate ownership is derived from the server-side session.

### POST /api/v1/shopping-agent/chat

Request body accepts `message` (1–1500 characters) and optional `mandate_id`.
The complete request body is capped at 8192 bytes. Unknown properties are
rejected. The model can call exactly three tools:

- `search_products`: optional keyword/category/merchant/price/currency filters.
- `get_product`: exact stable simulated catalogue `product_id`.
- `propose_purchase`: exact `product_id` plus quantity. Commercial facts are
  resolved from the local catalogue; the selected mandate is checked against
  the authenticated owner's active signed mandate.

Product prices and stock states are simulation fixtures and are never presented
as live merchant data. A proposal response includes separate proposal status
and deterministic policy decision fields. It does not issue an approval token,
create a PayPal order, or execute payment. Gemini is configured only on the
backend with `GEMINI_API_KEY` and optional `GEMINI_MODEL`.

### POST /api/v1/mandates/{mandate_id}/confirm
Explicit user confirmation of a `DRAFT` mandate, locking permissions.

- **Response:** `200 OK` (status updated to `CONFIRMED`)
- **Error:** `409 Conflict` if mandate is not in `DRAFT` status.
- **Error:** `400 Bad Request` if mandate has expired.

### POST /api/v1/mandates/{mandate_id}/sign
Generate server-side cryptographic HMAC-SHA256 signature for a `CONFIRMED` mandate.

**Response** `200 OK`
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "status": "SIGNED",
  "signature": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "signed_at": "2026-11-04T00:05:00Z",
  "signature_algorithm": "HMAC-SHA256",
  ...
}
```
**Error:** `409 Conflict` if mandate is in `DRAFT` (not confirmed) or already signed/terminal.

### POST /api/v1/mandates/{mandate_id}/activate
Verify cryptographic signature integrity and transition mandate from `SIGNED` to `ACTIVE`.

- **Response:** `200 OK` (status updated to `ACTIVE`)
- **Error:** `422 Unprocessable Entity` if signature verification fails (tampering detected).
- **Error:** `409 Conflict` if mandate is not in `SIGNED` status.

### POST /api/v1/mandates/{mandate_id}/revoke
Permanently revoke a mandate. Revocation is immediately terminal.

- **Response:** `200 OK` (status updated to `REVOKED`)
- **Error:** `409 Conflict` if already revoked.

### GET /api/v1/mandates/{mandate_id}/verify
Verify cryptographic validity, expiration, and active status of a mandate.

**Response** `200 OK`
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "valid": true,
  "status": "ACTIVE",
  "is_expired": false,
  "is_tampered": false,
  "reason": null
}
```

When invalid:
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "valid": false,
  "status": "EXPIRED",
  "is_expired": true,
  "is_tampered": false,
  "reason": "Mandate validity window has expired."
}
```

---

## Phase 2B Purchase Proposal Endpoints

A proposal is a purchase *request*. Creating or retrieving a proposal does not
authorize a purchase and does not execute payment.

### POST /api/v1/proposals
Submit a purchase proposal associated with exactly one existing mandate.

Subtotal and total are calculated server-side. Client-supplied totals, payment
fields, and unknown properties are rejected (`extra="forbid"`).

**Request Body**
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "product_id": "sku-100",
  "product_name": "Hardcover Notebook",
  "merchant": "amazon.com",
  "category": "books",
  "quantity": 1,
  "unit_price": "12.50",
  "currency": "USD",
  "tax": "1.00",
  "shipping": "2.00",
  "other_charges": "0.50",
  "product_source": "https://amazon.com/dp/sku-100",
  "idempotency_key": "agent-req-0001"
}
```

**Response** `201 Created` (new proposal) or `200 OK` (idempotent replay)
```json
{
  "proposal_id": "prp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "schema_version": "1.0",
  "status": "SUBMITTED",
  "mandate_id": "man_a1b2c3d4e5f67890",
  "product_id": "sku-100",
  "product_name": "Hardcover Notebook",
  "merchant": "amazon.com",
  "category": "books",
  "quantity": 1,
  "unit_price": "12.50",
  "currency": "USD",
  "subtotal": "12.50",
  "tax": "1.00",
  "shipping": "2.00",
  "other_charges": "0.50",
  "total": "16.00",
  "product_source": "https://amazon.com/dp/sku-100",
  "created_at": "2026-11-04T00:00:00+00:00",
  "idempotency_key": "agent-req-0001",
  "fingerprint": "…64-char sha256 hex…",
  "content_hash": "…64-char sha256 hex…"
}
```

**Errors**
- `404 Not Found` if the associated mandate does not exist.
- `409 Conflict` if `idempotency_key` is reused with different commercial terms.
- `422 Unprocessable Entity` for invalid money/quantity/currency/merchant values
  or when proposal currency does not match the mandate currency.

### GET /api/v1/proposals
List recent proposals.

**Query Parameters**
- `limit` (integer, default: 50, min: 1, max: 100)
- `mandate_id` (optional string) — restrict results to one mandate

**Response** `200 OK` (array of `Proposal` objects)
**Error:** `404 Not Found` if `mandate_id` is supplied and does not exist.
**Error:** `422 Unprocessable Entity` if a stored proposal fails integrity checks.

### GET /api/v1/proposals/{proposal_id}
Retrieve a proposal after content-hash verification.

- **Response:** `200 OK` (full `Proposal` object)
- **Error:** `404 Not Found` if the proposal does not exist.
- **Error:** `422 Unprocessable Entity` if stored terms do not match `content_hash`.

Proposal commercial terms are immutable after insert. Changed baskets require a
new proposal (and a new idempotency key when one is used). Evaluation/approval
status is recorded separately in later phases and is not a payment instruction.

---

## Proposal evaluation and Intent Auditor (Phase 5)

`POST /api/v1/proposals/{proposal_id}/evaluate` requires the owning authenticated
session. Deterministic policy executes first. BLOCK remains final. Other outcomes
are assessed by the no-tools Intent Auditor; LOW preserves policy behavior,
MEDIUM/HIGH remain in EVALUATED for user review, and UNCERTAIN/provider failure
returns a safe service-unavailable response without enabling approval or payment.
Assessment detail is bounded and does not expose provider prompts or secrets.

## Phase 3 PayPal Sandbox Checkout

### POST /api/v1/proposals/{proposal_id}/create-order

Requires an authenticated session and current durable purchase consent. Creates or
resumes the proposal's single PayPal Sandbox order attempt. Repeated requests reuse
the same provider idempotency key; an uncertain result never receives a new key. If
no provider ID is known, key replay is refused after the documented six-hour Orders
API idempotency window and the attempt remains pending operator reconciliation.

### GET /api/v1/proposals/{proposal_id}/checkout

Returns the authenticated owner's persisted attempt/order state for reload recovery:
status, amount/currency, and whether current consent exists. When current consent is
valid, provider order ID and approval URL may be returned. Otherwise those provider
identifiers are omitted; `recovery_available` indicates an anchored order can be
verified through the owner-scoped recovery operation. Consent hashes are never
returned.
The approval URL comes from PayPal's `approve` or `payer-action` response relation
and must be a verified PayPal Sandbox host.

### POST /api/v1/payments/return

Accepts proposal and order identifiers as untrusted browser references. The backend
checks local ownership and queries PayPal. Buyer approval is not payment settlement;
`payment_completed` is true only after server-verified capture with a real capture ID.

### POST /api/v1/proposals/{proposal_id}/checkout/reconcile

Requires the authenticated proposal owner and takes no browser-supplied order ID.
LEASH loads the durable attempt/order internally and fetches current provider state.
Only an independently verified completed capture is recorded as settlement. Other
provider states remain non-success and cannot authorize a capture through this
operation. The response contains proposal and status fields only, no PayPal order
ID, capture ID, or approval URL.

### POST /api/v1/payments/{order_id}/capture

Revalidates proposal, mandate, assessment, budget and durable consent. Ambiguous
outcomes become `RECONCILIATION_REQUIRED` and are never blindly recaptured.

Operational handling for expired or unresolved attempts is documented in
[`PAYMENT_OPERATIONS.md`](PAYMENT_OPERATIONS.md). It describes the limits of
existing recovery endpoints and does not define an operator API.

The current session model has no persistent account identity. A newly created
anonymous session cannot recover another principal's proposal; same-owner recovery
requires a valid session renewal or another active session for that principal.

## Phase 2C+ Endpoints (Planned)

### POST /api/v1/policies
User-level policy configuration and evaluation against an active mandate.

Full audit logs and chain-verification diagnostics are available only through the
admin-authenticated `/api/v1/admin/audit/...` endpoints. The user-facing,
owner-scoped display projection is `GET /api/v1/activity/overview` above.

### Payment execution
PayPal capture remains out of band from proposal creation.
