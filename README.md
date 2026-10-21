# ResearchPilot AI

A multi-agent research and report platform built with Flask, PostgreSQL and LangGraph.

> **Build status: all 13 phases complete.**
> The application is feature-complete for the specification but has **not** been run against the live
> LLM and search providers in the build environment; see [docs/FINAL_VERIFICATION.md](docs/FINAL_VERIFICATION.md) and
> [docs/TESTING.md](docs/TESTING.md). The legal documents are templates with operator placeholders and
> need legal review before real use; nothing in them claims legal compliance.

## What it does

You describe a research question (and optionally upload documents). A background run plans the work,
searches the web, reads pages, extracts claims tied to stored excerpts, writes a cited report, has it
reviewed and validated, and saves it. You can browse sources, edit the report, see transparent quality
checks, and export to Markdown, HTML, Word or PDF. Reports are AI-assisted drafts: check important claims
against the cited sources ([docs/legal/AI_USAGE_AND_LIMITATIONS.md](docs/legal/AI_USAGE_AND_LIMITATIONS.md)).

Research text is sent to the LLM and search providers you configure; see the privacy policy.

## Features

- Accounts: registration, email verification (real SMTP), login, password reset, preferences, data export (ZIP), scheduled account deletion
- LLM layer: Gemini, Groq, OpenRouter with key rotation, fallback and validated structured output
- Research: Tavily search, SSRF-safe fetching, HTML/PDF/DOCX/TXT reading, uploads, untrusted-content handling
- Multi-agent LangGraph workflow with critic/revise loop and citation validation
- Background runs (thread, Celery or manual) with live progress and resume
- Saved reports, history, dashboard from real data; source explorer; report editor with revisions
- Deterministic quality scoring and export gates; Markdown/HTML/Word/PDF export
- Security: CSRF, rate limits, strict CSP, hardened headers, secret-redacting logs
- Public `/legal` pages generated from `docs/legal/`
- Automated tests on PostgreSQL and SQLite (see [docs/TESTING.md](docs/TESTING.md))

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
| [docs/DATABASE_SETUP.md](docs/DATABASE_SETUP.md), [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) | PostgreSQL, migrations, tables |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Structure, request flow, security decisions |
| [docs/AGENT_WORKFLOW.md](docs/AGENT_WORKFLOW.md), [docs/AGENTS.md](docs/AGENTS.md) | The workflow and the agents |
| [docs/BACKGROUND_JOBS.md](docs/BACKGROUND_JOBS.md) | Runners, Celery, progress stream, recovery |
| [docs/API_KEY_ROTATION.md](docs/API_KEY_ROTATION.md) | Provider keys, rotation, fallback |
| [docs/API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md) | Routes, limits, CLI |
| [docs/EDITOR.md](docs/EDITOR.md), [docs/QUALITY_AND_EXPORTS.md](docs/QUALITY_AND_EXPORTS.md), [docs/ACCOUNT_DATA.md](docs/ACCOUNT_DATA.md) | Editor, scoring and exports, account data |
| [docs/SECURITY.md](docs/SECURITY.md) | Controls, threat model, incident response |
| [docs/TESTING.md](docs/TESTING.md), [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | Tests and contribution rules |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), [docs/BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md), [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Operating it |
| [docs/legal/](docs/legal/README.md) | Privacy, cookies, terms, acceptable use, AI limits, retention, security disclosure, contact (templates) |
| [docs/FINAL_VERIFICATION.md](docs/FINAL_VERIFICATION.md) | What was checked against the acceptance criteria, and what was not |
| [docs/CHANGELOG.md](docs/CHANGELOG.md), [docs/ROADMAP.md](docs/ROADMAP.md), [docs/PROGRESS.md](docs/PROGRESS.md) | History and plans |
