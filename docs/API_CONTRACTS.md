# LEASH API Contracts

Base URL: `http://localhost:8000`

---

## Phase 1 Foundation Endpoints

### GET /health
Health check endpoint.

**Response** `200 OK`
```json
{
  "status": "healthy",
  "timestamp": "2026-11-04T00:00:00.000000+00:00"
}
```

### GET /api/v1/status
System status endpoint.

**Response** `200 OK`
```json
{
  "status": "operational",
  "version": "0.1.0",
  "service": "LEASH"
}
```

---

## Phase 2A Mandate Endpoints

### POST /api/v1/mandates
Create a new purchase authorization mandate in `DRAFT` status.

**Request Body**
```json
{
  "max_total_budget": "40.00",
  "currency": "USD",
  "allowed_merchants": ["amazon.com", "target.com"],
  "allowed_categories": ["gifts", "books"],
  "max_item_count": 2,
  "max_quantity_per_item": 1,
  "max_price_per_item": "25.00",
  "allow_subscriptions": false,
  "require_user_approval": true,
  "expires_at": "2026-11-05T00:00:00Z"
}
```

**Response** `201 Created`
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "schema_version": "1.0",
  "status": "DRAFT",
  "max_total_budget": "40.00",
  "currency": "USD",
  "allowed_merchants": ["amazon.com", "target.com"],
  "allowed_categories": ["books", "gifts"],
  "max_item_count": 2,
  "max_quantity_per_item": 1,
  "max_price_per_item": "25.00",
  "allow_subscriptions": false,
  "require_user_approval": true,
  "created_at": "2026-11-04T00:00:00Z",
  "expires_at": "2026-11-05T00:00:00Z",
  "signature": null,
  "signed_at": null,
  "signature_algorithm": null
}
```

### GET /api/v1/mandates
List recent mandates.

**Query Parameters**
- `limit` (integer, default: 50, min: 1, max: 100)

**Response** `200 OK`
```json
[
  {
    "mandate_id": "man_a1b2c3d4e5f67890",
    "schema_version": "1.0",
    "status": "ACTIVE",
    ...
  }
]
```

### GET /api/v1/mandates/{mandate_id}
Retrieve details of a specific mandate by its ID.

- **Response:** `200 OK` (returns full `Mandate` object)
- **Error:** `404 Not Found` if mandate does not exist.

### POST /api/v1/mandates/{mandate_id}/confirm
Explicit user confirmation of a `DRAFT` mandate, locking permissions.

- **Response:** `200 OK` (status updated to `CONFIRMED`)
- **Error:** `409 Conflict` if mandate is not in `DRAFT` status.
- **Error:** `400 Bad Request` if mandate has expired.

### POST /api/v1/mandates/{mandate_id}/sign
Generate server-side cryptographic HMAC-SHA256 signature for a `CONFIRMED` mandate.

**Response** `200 OK`
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "status": "SIGNED",
  "signature": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "signed_at": "2026-11-04T00:05:00Z",
  "signature_algorithm": "HMAC-SHA256",
  ...
}
```
**Error:** `409 Conflict` if mandate is in `DRAFT` (not confirmed) or already signed/terminal.

### POST /api/v1/mandates/{mandate_id}/activate
Verify cryptographic signature integrity and transition mandate from `SIGNED` to `ACTIVE`.

- **Response:** `200 OK` (status updated to `ACTIVE`)
- **Error:** `422 Unprocessable Entity` if signature verification fails (tampering detected).
- **Error:** `409 Conflict` if mandate is not in `SIGNED` status.

### POST /api/v1/mandates/{mandate_id}/revoke
Permanently revoke a mandate. Revocation is immediately terminal.

- **Response:** `200 OK` (status updated to `REVOKED`)
- **Error:** `409 Conflict` if already revoked.

### GET /api/v1/mandates/{mandate_id}/verify
Verify cryptographic validity, expiration, and active status of a mandate.

**Response** `200 OK`
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "valid": true,
  "status": "ACTIVE",
  "is_expired": false,
  "is_tampered": false,
  "reason": null
}
```

When invalid:
```json
{
  "mandate_id": "man_a1b2c3d4e5f67890",
  "valid": false,
  "status": "EXPIRED",
  "is_expired": true,
  "is_tampered": false,
  "reason": "Mandate validity window has expired."
}
```

---

## Phase 2B+ Endpoints (Planned)

### POST /api/v1/agent/purchase-request
Submit a purchase request from an AI agent against an active mandate.

### POST /api/v1/policies
User-level policy configuration.

### GET /api/v1/audit/logs
Retrieve immutable audit logs.
