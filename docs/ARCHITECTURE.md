# LEASH Architecture

## Overview

LEASH is a security-focused agentic commerce platform that ensures AI purchasing agents operate within user-defined trust boundaries.

## System Components

```
┌──────────────────────────────────────────────────┐
│                    Frontend                       │
│              React + Vite (SPA)                   │
│         Dashboard · Policy Config · Logs          │
├──────────────────────────────────────────────────┤
│                    Backend                        │
│              FastAPI (REST API)                   │
│   Mandate Engine · Proposals · Policy · Payments │
├──────────────────────────────────────────────────┤
│                   Data Layer                      │
│     SQLite (dev) — extensible to PostgreSQL       │
│   Mandates · Proposals · Policies · Audit Logs   │
├──────────────────────────────────────────────────┤
│               External Services                   │
│         PayPal API · Gemini AI · (future)         │
└──────────────────────────────────────────────────┘
```

## Design Principles
1. **Deny by default** — No purchase executes without an explicit matching mandate and policy.
2. **Deterministic authorization** — AI agents propose purchases, but deterministic code authorizes transactions.
3. **Auditability & Integrity** — Every mandate is cryptographically signed and stored immutably.
4. **Separation of concerns** — AI reasoning, mandate enforcement, and payment execution are strictly isolated.

## API Architecture
- RESTful JSON API under `/api/v1/`.
- Pydantic models with strict validation (`extra="forbid"`).
- Exact Decimal monetary types and UTC timestamps.

## Current State (Phase 5 Intent Auditor)
- Foundation, mandate, proposal, policy, audit, secure-session, and PayPal Sandbox phases are implemented.
- SQLite database schema versioning (Version 11).
- Mandate subsystem operational:
  - Strict Pydantic validation (budget, currency, merchants, expiration).
  - Deterministic canonical JSON serialization.
  - Server-side HMAC-SHA256 mandate signing & constant-time verification.
  - Lifecycle state machine (`DRAFT` → `CONFIRMED` → `SIGNED` → `ACTIVE`, plus `EXPIRED` & `REVOKED`).
  - REST endpoints under `/api/v1/mandates`.
- Purchase proposal subsystem operational (request only — not authorization or payment):
  - Strict Pydantic validation with server-calculated subtotal/total.
  - One proposal : one mandate, matching currency, canonical merchant identity.
  - Untrusted product fields stored as opaque text (never executed or fetched).
  - Write-once commercial terms, duplicate protection via fingerprint and idempotency key.
  - REST endpoints under `/api/v1/proposals` (create, get, list).
- Shopping Agent (`/api/v1/shopping-agent`):
  - Backend-only Gemini REST integration using existing `httpx` and `GEMINI_API_KEY`.
  - Exactly three model tools: `search_products`, `get_product`, and `propose_purchase`.
  - Deterministic local catalogue with explicitly simulated prices and availability.
  - Proposal tool accepts only a catalogue product ID and quantity; ownership, active mandate signature, expiry, kill switch, and deterministic policy are enforced server-side.
  - Agent modules have no approval-token or payment-executor imports. Proposal evaluation is not payment execution.
  - Product descriptions are HTML-stripped, capped, and heuristically checked for prompt-injection indicators; the heuristic is defense in depth, not a safety guarantee.

## Intent Auditor (Phase 5)
- The deterministic P01–P12 policy engine runs first. A BLOCK is persisted and returned without an auditor call.
- Non-BLOCK proposals remain EVALUATED while an independent Gemini client with zero tools reviews minimized structured mandate/proposal summaries.
- LOW risk preserves the deterministic outcome; MEDIUM/HIGH require the existing authenticated user approval-token lifecycle; UNCERTAIN or unavailable assessment holds progression.
- Assessments bind immutable proposal content, canonical mandate permissions, and deterministic policy result hashes. Approval and payment services independently validate current bindings.

## Phase 7G PayPal checkout reliability
- One `paypal_order_attempts` row is committed before any create-order request. It is unique per proposal and binds the authenticated owner, durable consent ID/binding, exact amount/currency, and a stable PayPal request ID.
- Attempt flow: `PREPARED` → `REQUESTING` → `UNKNOWN` (provider response received or outcome ambiguous) → `PROVIDER_CREATED`; failures requiring operator attention remain `RECONCILIATION_REQUIRED`. A process crash in `REQUESTING` is recovered by replaying the exact request with the same persisted PayPal idempotency key or looking up the recorded provider order ID. No new key is generated for that attempt.
- SQLite write transactions end before PayPal network calls. The returned provider order ID and approval URL are anchored in the attempt row before local order finalization. If current authorization has changed, the order is recorded failed/reconciliation-only and cannot be captured.
- A new capture requires current authorization and consent, reserves `CAPTURING` transactionally, appends a capture-attempt audit event, and releases the write lock before PayPal I/O. An ambiguous outcome is reconciled with provider lookup; it is never blindly recaptured. A separate completed-capture reconciliation verifies the exact order, amount/currency, and real capture ID, then records the external financial fact atomically even if authorization later expires. This reconciliation never dispatches a capture or grants new authorization.
- PayPal `PayPal-Request-Id` deduplication is used with the exact durable request payload. The current Orders API documentation describes a six-hour key retention window (longer retention requires provider/account support); LEASH refuses key replay after six hours and keeps the attempt blocked for manual reconciliation rather than minting a new attempt. See [PayPal Orders API idempotency guidance](https://developer.paypal.com/api/rest/integration/orders-api/api-use-cases/advanced/).
- Browser return parameters are references only. The authenticated backend verifies the local order and fetches PayPal state before recording approval or settlement. The frontend never treats redirect state as success.
- The adapter accepts the PayPal Orders v2 `approve` and `payer-action` HATEOAS relations only when their URLs pass the allowlisted Sandbox host checks. See [PayPal Orders v2 create-order response](https://developer.paypal.com/sdk/orders/v2/orders-create/).
- Migration 8 stores bounded structured assessments and audit events; raw prompts and model response text are not persisted.
