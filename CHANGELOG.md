# Changelog

All notable changes to LEASH will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0-dev] — 2026-11-04

### Added
- **Mandate Data Model:** Strict Pydantic models (`MandateCreate`, `Mandate`, `MandateVerificationResponse`) utilizing `Decimal` for financial accuracy and timezone-aware UTC timestamps.
- **Deterministic Canonical Serialization:** Canonical JSON serializer (`canonicalize_mandate_payload`) ensuring stable key ordering, sorted merchant/category lists, fixed Decimal formatting, and compact separators.
- **Cryptographic Signing:** Server-side HMAC-SHA256 signing (`sign_payload`) and constant-time verification (`verify_signature`). The signing key is loaded only from environment configuration (no source-code default) and must be at least 32 bytes.
- **Mandate Lifecycle Engine:** Deterministic state machine supporting `DRAFT` → `CONFIRMED` → `SIGNED` → `ACTIVE`, with terminal `EXPIRED` and `REVOKED` states.
- **Database Schema Version 2:** SQLite migration adding the `mandates` table with immutable permissions enforcement on conflicts.
- **Mandate REST API:** Endpoints under `/api/v1/mandates` for creating, confirming, signing, activating, verifying, revoking, and listing mandates.
- **Test Suite Expansion:** Schema validation, signing/tamper resistance, lifecycle, API, persistence, and signing-key configuration tests (72 backend tests).

## [0.1.0] — 2026-11-03

### Added
- Project foundation and directory structure.
- FastAPI backend with health and status endpoints.
- SQLite database initialization with schema versioning.
- React/Vite frontend with backend connectivity check.
- Environment-based configuration with `.env.example`.
- CORS configuration for local development.
- Pytest test suite for backend endpoints.
- GitHub Actions CI workflow for backend tests.
- Project documentation: ARCHITECTURE.md, API_CONTRACTS.md, DATABASE_SCHEMA.md, SECURITY_MODEL.md, DEVELOPMENT_ROADMAP.md, DECISION_LOG.md.
- AGENTS.md with coding conventions and security rules.
