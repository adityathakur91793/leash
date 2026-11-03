# AGENTS.md — LEASH Coding Conventions

## Project Identity
- **Project**: LEASH — The Trust Layer for Agentic Commerce
- **Repository**: https://github.com/adityathakur91793/leash

## Architecture
- **Frontend**: React + Vite + JavaScript + Custom CSS + Lucide React
- **Backend**: Python 3.11+ + FastAPI + Pydantic + SQLite
- **Testing**: Pytest + HTTPX (backend), Vitest (frontend — future)

## Directory Conventions
- Backend code lives in `backend/app/`
- Frontend code lives in `frontend/src/`
- Tests live in `backend/tests/` and `frontend/src/__tests__/` (future)
- Documentation lives in `docs/`

## Coding Standards

### Python (Backend)
- Use type hints on all function signatures.
- Use `async def` for all endpoint handlers and database operations.
- Use Pydantic models for request/response schemas in `backend/app/schemas/`.
- Use `pydantic-settings` for configuration in `backend/app/core/config.py`.
- Follow PEP 8. Maximum line length: 99 characters.
- Use Python logging (not print statements).
- Docstrings: Google style.

### JavaScript (Frontend)
- Use functional components with hooks.
- Use named exports (not default) for components, except `App.jsx`.
- API calls go in `frontend/src/services/`.
- Custom hooks go in `frontend/src/hooks/`.
- Environment variables must use the `VITE_` prefix.

## Security Rules (MANDATORY)
1. **Never hardcode API keys, secrets, or credentials** in source code.
2. **Never commit `.env` files** — only `.env.example` with placeholder values.
3. **Never expose backend secrets to frontend code** — only `VITE_`-prefixed env vars are bundled.
4. **Never create fake payment success flows** — payment must be real or explicitly stubbed with clear warnings.
5. **Never store actual payment credentials or PII** in the database during development.
6. **Always validate and sanitize** user inputs on both frontend and backend.
7. **Always use parameterized queries** — never construct SQL with string concatenation.
8. **Use environment-based configuration** for all external service connections.

## Git Conventions
- Commit messages: `type(scope): description` (e.g., `feat(backend): add health endpoint`)
- Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `security`
- Branch naming: `phase-N/description` (e.g., `phase-1/foundation`)

## Testing
- All backend endpoints must have corresponding tests.
- Use `pytest-asyncio` for async test functions.
- Use HTTPX `AsyncClient` for API integration tests.
- Test database should use a temporary file, not the production database.
