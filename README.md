# leash
The Trust Layer for Agentic Commerce — secure AI-powered purchasing with deterministic authorization and PayPal integration.

---

## Overview

**LEASH** is a security-focused agentic commerce platform built for the PayPal AI Hackathon. Its core mission is to establish a deterministic trust boundary for autonomous AI purchasing agents. By enforcing user-defined policies, anomaly detection, and cryptographic purchase mandates, LEASH ensures that AI agents can never execute transactions outside explicit human consent.

## Architecture

- **Frontend**: React (Vite), JavaScript, Custom CSS, Lucide React icons
- **Backend**: Python 3.11+, FastAPI, Pydantic, SQLite (aiosqlite)
- **Testing**: Pytest, HTTPX (async ASGI client)
- **CI/CD**: GitHub Actions

## Project Structure

```
leash/
├── AGENTS.md                  # Conventions & security rules for coding agents
├── README.md                  # Project overview & setup instructions
├── CHANGELOG.md               # Version history and notes
├── LICENSE                    # MIT License
├── .gitignore                 # Environment, build, and secret exclusions
├── .env.example               # Root configuration template
├── docs/                      # Architectural & design specifications
│   ├── ARCHITECTURE.md
│   ├── API_CONTRACTS.md
│   ├── DATABASE_SCHEMA.md
│   ├── SECURITY_MODEL.md
│   ├── DEVELOPMENT_ROADMAP.md
│   └── DECISION_LOG.md
├── backend/                   # FastAPI backend service
│   ├── app/
│   │   ├── main.py            # Application entrypoint & lifespan
│   │   ├── core/              # Config & security utilities
│   │   ├── api/v1/            # API version 1 endpoints
│   │   ├── models/            # Data models
│   │   ├── schemas/           # Pydantic schemas
│   │   ├── services/          # Business logic services
│   │   └── db/                # SQLite database management
│   ├── tests/                 # Pytest test suite
│   ├── requirements.txt       # Python dependencies
│   └── pyproject.toml         # Tooling configuration
├── frontend/                  # React / Vite application
│   ├── src/
│   │   ├── components/        # Reusable UI components
│   │   ├── hooks/             # Custom React hooks
│   │   ├── services/          # API integration layer
│   │   ├── pages/             # Route pages
│   │   ├── styles/            # Styling assets
│   │   ├── App.jsx            # Main app shell & status check
│   │   └── main.jsx           # React DOM root
│   ├── package.json
│   └── vite.config.js
├── scripts/                   # Utility & deployment scripts
└── .github/                   # Workflows & CI automation
    └── workflows/
        └── tests.yml
```

## Getting Started

### Prerequisites

- Python 3.11 or higher
- Node.js 18 or higher (v20+ recommended)
- npm 9+

### Backend Setup

1. Navigate to the backend directory:
   ```bash
   cd backend
   ```

2. Create a virtual environment and activate it:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure environment variables (required for mandate signing):
   ```bash
   cp ../.env.example .env
   ```
   Set `MANDATE_SIGNING_KEY` to a random secret of at least 32 bytes. The backend
   will not start if the key is missing or too short.

5. Run the development server:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

6. Verify the backend health:
   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/api/v1/status
   ```

### Running Backend Tests

With the virtual environment activated:
```bash
pytest tests/ -v
```

### Frontend Setup

1. Navigate to the frontend directory:
   ```bash
   cd frontend
   ```

2. Install dependencies:
   ```bash
   npm install
   ```

3. Configure environment variables (optional):
   ```bash
   cp .env.example .env
   ```

4. Start the Vite development server:
   ```bash
   npm run dev
   ```

5. Build for production:
   ```bash
   npm run build
   ```

## Security & Coding Standards

Please refer to [`AGENTS.md`](./AGENTS.md) for strict coding conventions, secret management rules, and development guidelines.

## License

This project is licensed under the MIT License — see the [`LICENSE`](./LICENSE) file for details.
