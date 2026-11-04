# LEASH Database Schema

## Current Schema (Phase 2A — Version 2)

### 1. `schema_version`
Tracks database migration version history.

| Column      | Type    | Constraints | Description                     |
|-------------|---------|-------------|---------------------------------|
| `version`   | INTEGER | NOT NULL    | Schema migration version number |
| `applied_at`| TEXT    | NOT NULL    | ISO 8601 UTC timestamp         |

### 2. `mandates`
Stores user-granted purchase authorization mandates, cryptographic signatures, and lifecycle states.

| Column                  | Type    | Constraints | Description                                                  |
|-------------------------|---------|-------------|--------------------------------------------------------------|
| `mandate_id`            | TEXT    | PRIMARY KEY | Unique identifier prefix `man_`                              |
| `schema_version`        | TEXT    | NOT NULL    | Mandate specification version (e.g. `1.0`)                   |
| `status`                | TEXT    | NOT NULL    | `DRAFT`, `CONFIRMED`, `SIGNED`, `ACTIVE`, `EXPIRED`, `REVOKED`|
| `currency`              | TEXT    | NOT NULL    | ISO 4217 code (e.g. `USD`, `EUR`)                            |
| `max_total_budget`      | TEXT    | NOT NULL    | Exact Decimal formatted as string (`40.00`)                 |
| `max_price_per_item`    | TEXT    | NULLABLE    | Optional per-item spending cap (`25.00`)                     |
| `max_item_count`        | INTEGER | NOT NULL    | Maximum total items permitted                                |
| `max_quantity_per_item` | INTEGER | NOT NULL    | Maximum quantity permitted for any single item               |
| `allow_subscriptions`   | INTEGER | NOT NULL    | Boolean flag (0=disallow, 1=allow)                           |
| `require_user_approval` | INTEGER | NOT NULL    | Boolean flag (0=automatic, 1=require sign-off)               |
| `allowed_merchants`     | TEXT    | NOT NULL    | JSON string array of normalized domains                      |
| `allowed_categories`    | TEXT    | NOT NULL    | JSON string array of normalized category tags                |
| `created_at`            | TEXT    | NOT NULL    | ISO 8601 UTC creation timestamp                              |
| `expires_at`            | TEXT    | NOT NULL    | ISO 8601 UTC expiration timestamp                            |
| `signature`             | TEXT    | NULLABLE    | 64-character HMAC-SHA256 hex digest                          |
| `signed_at`             | TEXT    | NULLABLE    | ISO 8601 UTC signing timestamp                               |
| `signature_algorithm`   | TEXT    | NULLABLE    | Cryptographic signing algorithm (`HMAC-SHA256`)              |

---

## Planned Tables (Phase 2B+)

### `purchase_requests`
AI agent purchase attempts submitted for evaluation against active mandates.

### `policies`
Standing user constraints and authorization policies.

### `audit_logs`
Cryptographically hash-chained append-only record of all agent proposals and authorization decisions.
