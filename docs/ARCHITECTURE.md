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
│   Mandate Engine · Policy · Audit · Payments     │
├──────────────────────────────────────────────────┤
│                   Data Layer                      │
│     SQLite (dev) — extensible to PostgreSQL       │
│   Mandates · Policies · Audit Logs · Sessions    │
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

## Current State (Phase 2A Complete)
- Foundation operational (health and status probes).
- SQLite database schema versioning (Version 2).
- Mandate subsystem operational:
  - Strict Pydantic validation (budget, currency, merchants, expiration).
  - Deterministic canonical JSON serialization.
  - Server-side HMAC-SHA256 mandate signing & constant-time verification.
  - Lifecycle state machine (`DRAFT` → `CONFIRMED` → `SIGNED` → `ACTIVE`, plus `EXPIRED` & `REVOKED`).
  - REST endpoints under `/api/v1/mandates`.
