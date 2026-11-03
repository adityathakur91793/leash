# LEASH API Contracts

Base URL: `http://localhost:8000`

## Phase 1 Endpoints

### GET /health
Health check endpoint.

**Response** `200 OK`
```json
{
  "status": "healthy",
  "timestamp": "2026-11-03T12:00:00.000000"
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

## Phase 2+ Endpoints (Planned)

### POST /api/v1/policies
Create a purchase policy.

### GET /api/v1/policies
List all policies for the authenticated user.

### POST /api/v1/agent/purchase-request
Submit a purchase request from an AI agent for policy evaluation.

### GET /api/v1/audit/logs
Retrieve audit logs.

---

*Detailed request/response schemas will be added as endpoints are implemented.*
