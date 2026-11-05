# LEASH Decision Log

## DL-001: SQLite over PostgreSQL
**Date**: 2026-11-03
**Decision**: Use SQLite for the database.
**Rationale**: Simplifies development and deployment for hackathon scope. No separate database server needed. Schema is designed to be portable to PostgreSQL if needed later.
**Trade-offs**: No concurrent write support. Acceptable for single-user demo.

## DL-002: Raw aiosqlite over SQLAlchemy
**Date**: 2026-11-03
**Decision**: Use aiosqlite directly instead of SQLAlchemy ORM.
**Rationale**: Reduces dependency complexity. The schema is simple enough that an ORM adds unnecessary abstraction at this stage.
**Trade-offs**: Manual query writing. Migration logic must be custom.

## DL-003: Vite over Create React App
**Date**: 2026-11-03
**Decision**: Use Vite as the frontend build tool.
**Rationale**: Faster HMR, smaller bundle, actively maintained. CRA is deprecated.
**Trade-offs**: None significant.

## DL-004: Custom CSS over Tailwind/MUI
**Date**: 2026-11-03
**Decision**: Use custom CSS for styling.
**Rationale**: Avoids large dependency. Full control over design for hackathon demo. Keeps bundle small.
**Trade-offs**: More manual styling work.

## DL-005: Proposals are requests, not authorizations
**Date**: 2026-11-04
**Decision**: Phase 2B introduces a purchase proposal model that only creates,
retrieves, and lists agent purchase requests.
**Rationale**: Authorization must remain deterministic mandate/policy code, and
payment must remain a later PayPal step. Mixing those into proposal creation
would collapse the trust boundary.
**Trade-offs**: Evaluation status exists on the model for later phases, but
proposal APIs in 2B do not approve purchases or capture payment.


## DL-006: Deterministic Policy Engine Security Architecture (Phase 2C)
**Date**: 2026-11-04
**Decision**: The Policy Engine executes 100% deterministic, side-effect-free Python logic without LLMs or external network calls.
- **Budget Ceilings are Immutable**: User approval of individual purchase proposals cannot expand or override the mandate's signed `max_total_budget` (Rule P03 is a hard block). Modifying budget requires cryptographically resigning a new mandate.
- **One-Time Exceptions Never Mutate Mandates**: Exceptions for unlisted merchants (P05) or categories (P06) produce single-use authorization tokens bound to the specific proposal. They never alter the signed mandate allowlists.
- **Empty Allowlist is Fail-Closed**: An empty `allowed_categories` list permits zero categories (Rule P06 yields `BLOCK`).
- **Live-Price Evidence Required**: When a live-price oracle is unavailable, stale, or unindexed, Rule P09 evaluates to `NOT_APPLICABLE` (never a false `PASS`).
- **Atomic Spend Reservation**: Concurrency races on daily transaction limits (Rule P10) are mitigated by evaluating and writing spend reservations inside a SQLite `BEGIN IMMEDIATE` transaction, accounting for proposals in `SUBMITTED`, `EVALUATED`, and `APPROVED` states.
- **Idempotent Replay**: Replaying an identical proposal or idempotency key returns the existing proposal record without creating duplicate authorizations or spend reservations.
- **Defense-in-Depth for Untrusted Text**: Rule P12 enforces strict structural integrity, rejects URI credentials, and blocks SSRF target hosts. Prompt injection patterns are flagged as audit heuristics, while true safety is guaranteed by treating product copy as opaque literal text that is never interpreted or executed.
**Rationale**: Eliminates non-deterministic LLM hallucinations and prompt injections from the trust boundary, ensuring mathematical enforceability of user-granted purchase authority.
**Trade-offs**: Policy engine requires structured schema fields and does not support fuzzy natural-language purchase rules.
