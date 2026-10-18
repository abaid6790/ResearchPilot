# ResearchPilot AI

A multi-agent research and report platform built with Flask, PostgreSQL and LangGraph.

> **Build status: Phases 0-6 of 13 complete.** The foundation, the account system, the LLM
> provider layer (with key rotation and fallback), the research/report database schema and the
> research tools (Tavily search, safe page fetching, document reading, uploads) work and are tested.
> The research agents and workflow work from the command line (`flask run-research`). The web "Start research" button, background runs, report editor and exports are **not built yet** (see [docs/PROGRESS.md](docs/PROGRESS.md)). Nothing in the UI pretends otherwise.

## What works today

- Flask application factory with validated configuration and clear startup errors
- PostgreSQL models and Alembic migrations
- Registration, email verification (real SMTP), login/logout, password reset, password change
- Single-use, expiring, hashed email tokens; sessions invalidated on password change
- CSRF protection, rate limiting, strict CSP, security headers, secret-redacting logs
- Responsive light/dark interface, honest empty states
- `/health` endpoint (app + database only)
- LLM provider layer: Gemini, Groq and OpenRouter adapters, key rotation, cross-provider fallback,
  bounded retries, structured-output validation (`docs/API_KEY_ROTATION.md`). Tested against a
  local fake server, not yet against the live services
- Research data model: projects, jobs, tasks, sources, evidence, claims, reports, revisions,
  quality evaluations, usage records, audit events, with ownership checks (`docs/DATABASE_SCHEMA.md`)
- Research workspace: create a project with every setting from the spec (validated on the server), list/search, project page, archive, delete with title confirmation, dashboard counts from the database. Runs cannot be started yet
- 227 automated tests, run against PostgreSQL and SQLite

## Quick start (Linux / macOS)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then edit: SECRET_KEY, DATABASE_URL, MAIL_*
python -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY
flask --app app db upgrade
flask --app app run --debug
```

Windows PowerShell, PostgreSQL setup, and local email testing are covered in
[docs/INSTALLATION.md](docs/INSTALLATION.md). Check your setup any time with:

```bash
python scripts/check_env.py          # config, PostgreSQL, Redis
python scripts/check_env.py --live   # also verifies each API key with the provider
```

## Documentation

| Document | Contents |
|---|---|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Windows and Linux/macOS setup, running, testing |
| [docs/ENVIRONMENT_SETUP.md](docs/ENVIRONMENT_SETUP.md) | Every environment variable |
| [docs/DATABASE_SETUP.md](docs/DATABASE_SETUP.md) | PostgreSQL, migrations, troubleshooting |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Structure, request flow, security decisions |
| [docs/AGENTS.md](docs/AGENTS.md) | The agents, workflow, guarantees and limits |
| [docs/SECURITY.md](docs/SECURITY.md) | SSRF, prompt-injection and upload handling, and what is not covered |
| [docs/PROGRESS.md](docs/PROGRESS.md) | Phase status and known limitations |
