# LEASH Database Schema

## Current Schema (Phase 2B — Version 3)

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

### 3. `proposals`
Stores agent purchase *requests*. Rows are write-once for commercial terms.
Status may later record evaluation outcome; it is not a payment order.

| Column            | Type    | Constraints | Description                                              |
|-------------------|---------|-------------|----------------------------------------------------------|
| `proposal_id`     | TEXT    | PRIMARY KEY | Unique identifier prefix `prp_`                          |
| `schema_version`  | TEXT    | NOT NULL    | Proposal specification version (e.g. `1.0`)              |
| `status`          | TEXT    | NOT NULL    | `SUBMITTED`, `EVALUATED`, `APPROVED`, `REJECTED`, `WITHDRAWN` |
| `mandate_id`      | TEXT    | NOT NULL, FK → `mandates.mandate_id` | Exactly one associated mandate |
| `product_id`      | TEXT    | NOT NULL    | Opaque merchant product identifier                       |
| `product_name`    | TEXT    | NOT NULL    | Untrusted product title (literal text only)              |
| `merchant`        | TEXT    | NOT NULL    | Canonical merchant identifier                            |
| `category`        | TEXT    | NOT NULL    | Normalized category tag                                  |
| `quantity`        | INTEGER | NOT NULL    | Item quantity (1–10000)                                  |
| `unit_price`      | TEXT    | NOT NULL    | Exact Decimal string                                     |
| `currency`        | TEXT    | NOT NULL    | ISO 4217 code; must match mandate currency               |
| `subtotal`        | TEXT    | NOT NULL    | Server-calculated `quantity * unit_price`                |
| `tax`             | TEXT    | NOT NULL    | Tax amount (zero allowed)                                |
| `shipping`        | TEXT    | NOT NULL    | Shipping amount (zero allowed)                           |
| `other_charges`   | TEXT    | NOT NULL    | Additional charges (zero allowed)                        |
| `total`           | TEXT    | NOT NULL    | Server-calculated `subtotal + tax + shipping + other_charges` |
| `product_source`  | TEXT    | NOT NULL    | Untrusted catalog URL/reference; never fetched           |
| `created_at`      | TEXT    | NOT NULL    | ISO 8601 UTC creation timestamp                          |
| `idempotency_key` | TEXT    | NULLABLE, UNIQUE (partial) | Optional client duplicate key          |
| `fingerprint`     | TEXT    | NOT NULL, UNIQUE | SHA-256 identity hash of commercial terms           |
| `content_hash`    | TEXT    | NOT NULL    | SHA-256 integrity hash of canonical proposal JSON        |

Migration 3 is additive: existing `mandates` rows and schema version 2 history
are preserved. Foreign keys are enforced (`PRAGMA foreign_keys = ON`).

---

## Planned Tables (Phase 2C+)

### `intent_assessments` (Migration 8)
Bounded auditor result and exact proposal, mandate-permission, and policy snapshot
hashes, schema/prompt/model versions, fixed finding codes, status, and timestamps.
Raw prompts and unrestricted provider output are not stored. Migration is
forward-only and uses `CREATE TABLE/INDEX IF NOT EXISTS`.

### `policies`
Standing user constraints and authorization policies.

### `audit_logs`
Cryptographically hash-chained append-only record of all agent proposals and authorization decisions.
