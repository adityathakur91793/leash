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

## Current State (Phase 4 Shopping Agent)
- Foundation, mandate, proposal, policy, audit, secure-session, and PayPal Sandbox phases are implemented.
- SQLite database schema versioning (Version 7).
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
