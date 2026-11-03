# LEASH Database Schema

## Current Schema (Phase 1)

### schema_version
Tracks database migration versions.

| Column      | Type    | Constraints | Description                    |
|-------------|---------|-------------|--------------------------------|
| version     | INTEGER | NOT NULL    | Schema version number          |
| applied_at  | TEXT    | NOT NULL    | ISO 8601 timestamp of migration|

## Planned Tables (Phase 2+)

### users
User accounts and authentication.

### policies
Purchase authorization policies defining spending limits, allowed categories, time windows.

### purchase_requests
AI agent purchase requests pending evaluation.

### audit_logs
Immutable log of all agent actions and policy decisions.

### mandates
Cryptographically signed purchase authorizations.

---

*Tables will be added incrementally with corresponding schema version bumps.*
