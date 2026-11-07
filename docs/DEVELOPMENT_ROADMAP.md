# LEASH Development Roadmap

## Phase 1: Foundation ✅
- FastAPI, React/Vite, health endpoints, SQLite, CI, and environment configuration.

## Phase 2A–2D: Trust and Policy ✅
- Signed, immutable mandates; purchase proposals; deterministic policy evaluation;
  tamper-evident audit events; emergency kill switch; authenticated sessions and
  immutable mandate ownership (Migration 6).

## Phase 3: PayPal Sandbox Executor ✅
- Sandbox-only PayPal client and payment executor with capture reconciliation,
  ownership checks, explicit user approval boundary, and Migration 7.
- Remote GitHub Actions confirmation was pending at the last checkpoint.

## Phase 4: AI Shopping Agent (implementation in review)
- [x] Strict product, request, response, and tool argument schemas.
- [x] Explicitly simulated local product catalogue and deterministic search.
- [x] Product HTML stripping, content limits, and heuristic injection quarantine.
- [x] Backend-only Gemini REST client with a fixed three-tool allowlist.
- [x] Authenticated agent endpoints; ownership, mandate signature/expiry,
  emergency control, and deterministic policy checks for proposals.
- [x] React Shopping Agent interface with simulated-data and policy outcome labels.
- [x] Focused security tests and Phase 4 documentation.

## Phase 5: Intent Auditor (implementation in review)
- [x] Independent no-tools auditor with strict fixed output schema.
- [x] Deterministic BLOCK-before-AI lifecycle and EVALUATED hold until assessment.
- [x] Content-bound assessment records and hash-chain audit events (Migration 8).
- [x] Independent assessment checks at approval and payment boundaries.
- [x] MEDIUM/HIGH require existing user approval; UNCERTAIN/outages fail closed.
- [x] Focused tests and architecture/security/API/schema documentation.

## Phase 6: Future work
- Not started.
