# LEASH Security Model

## Core Principle
**No AI agent can execute a purchase without explicit, verifiable human authorization.**

## Trust Architecture

### Layers of Defense
1. **Policy Layer** — User-defined rules that constrain what an agent can purchase.
2. **Intent Auditor** — AI-powered analysis of agent purchase requests for anomalies.
3. **Mandate Signing** — Cryptographic proof that a human authorized a specific transaction.
4. **Audit Trail** — Immutable log of every action for post-hoc review.

### Threat Model
| Threat                          | Mitigation                                    |
|---------------------------------|-----------------------------------------------|
| Agent exceeds spending limit    | Policy engine enforces hard limits             |
| Agent purchases unauthorized items | Category and merchant allowlists           |
| Prompt injection in agent       | Intent auditor detects anomalous requests      |
| Credential theft                | No credentials stored in agent context         |
| Unauthorized policy changes     | Authentication required for policy management  |

## Current Implementation (Phase 1)
- Environment-based configuration (no hardcoded secrets).
- `.env` excluded from version control.
- CORS restricted to known frontend origin.
- Input sanitization utilities.

## Planned (Phase 2+)
- Policy engine with deny-by-default evaluation.
- Purchase mandate signing and verification.
- Gemini-powered intent auditing.
- Comprehensive audit logging.
