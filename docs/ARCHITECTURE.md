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
│    Auth · Policy Engine · Audit · Payments        │
├──────────────────────────────────────────────────┤
│                   Data Layer                      │
│     SQLite (dev) — extensible to PostgreSQL       │
│   Policies · Audit Logs · Sessions · Mandates     │
├──────────────────────────────────────────────────┤
│               External Services                   │
│         PayPal API · Gemini AI · (future)          │
└──────────────────────────────────────────────────┘
```

## Design Principles
1. **Deny by default** — No purchase executes without an explicit matching policy.
2. **Auditability** — Every agent action and policy decision is logged.
3. **Separation of concerns** — AI reasoning, policy evaluation, and payment execution are independent modules.
4. **Defense in depth** — Multiple validation layers prevent unauthorized purchases.

## API Architecture
- RESTful JSON API under `/api/v1/`.
- Versioned endpoints to support backward compatibility.
- Pydantic models for request/response validation.

## Current State (Phase 1)
- Health and status endpoints operational.
- SQLite database initialization with schema versioning.
- Frontend connectivity verification.
- Foundation ready for Phase 2 feature implementation.
