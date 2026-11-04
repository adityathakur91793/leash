# LEASH Development Roadmap

## Phase 1: Foundation ✅
- [x] Project structure and tooling
- [x] FastAPI backend with health/status endpoints
- [x] SQLite database initialization
- [x] React/Vite frontend with connectivity check
- [x] CI/CD pipeline (GitHub Actions)
- [x] Documentation foundation
- [x] Configuration hardening (CORS, SQLite paths, env discovery)

## Phase 2A: Mandate Model & Secure Signing ✅
- [x] Strict Pydantic mandate schema (`Decimal` currency, domains, limits)
- [x] Deterministic canonical JSON serialization
- [x] Server-side HMAC-SHA256 mandate signing & constant-time verification
- [x] Mandate lifecycle engine (`DRAFT` → `CONFIRMED` → `SIGNED` → `ACTIVE` / `EXPIRED` / `REVOKED`)
- [x] SQLite persistence and migration to Schema Version 2
- [x] RESTful Mandate API endpoints (`/api/v1/mandates`)
- [x] Comprehensive test suite (schema, signing, lifecycle, API)

## Phase 2B: Purchase Proposal Model ✅
- [x] Strict Pydantic purchase proposal schema (`Decimal` money, UTC timestamps)
- [x] Server-side subtotal/total recalculation
- [x] Mandate association (exactly one), currency consistency, canonical merchants
- [x] Untrusted product content handled as opaque data
- [x] Write-once commercial terms, content-hash integrity, duplicate/idempotency protection
- [x] SQLite migration to Schema Version 3 (`proposals` table)
- [x] REST endpoints (`POST/GET /api/v1/proposals`)
- [x] Tests for validation, duplicates, tampering, persistence, and mandate compatibility

## Phase 2C: Core Trust Engine & Policy Evaluation (Next)
- [ ] Policy data model and CRUD API
- [ ] Policy evaluation engine (deny-by-default against active mandate)
- [ ] Proposal evaluation flow (status transitions only; no payment execution)
- [ ] Basic audit logging

## Phase 3: AI Shopping Agent & Security Hardening
- [ ] AI shopping agent (Gemini integration)
- [ ] Intent auditor (Gemini-powered anomaly detection)
- [ ] Attack Lab (demonstrate and defend against prompt injection)
- [ ] Rate limiting and abuse prevention

## Phase 4: Payment Integration
- [ ] PayPal SDK integration (sandbox)
- [ ] End-to-end purchase flow with policy enforcement
- [ ] Transaction receipts and confirmation

## Phase 5: Dashboard and Demo
- [ ] Full dashboard UI with mandate and policy management
- [ ] Real-time audit log viewer
- [ ] Demo scenario walkthroughs
- [ ] Hackathon presentation materials
