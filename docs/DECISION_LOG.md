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
