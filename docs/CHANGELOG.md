# Changelog

Phases are the project's build milestones, not released versions. No version number has been tagged.

## Phase 13 - Hardening and final verification
* Default models updated after real-key checks (2026-10-20): Gemini `gemini-flash-lite-latest` (fast, confirmed) and `gemini-flash-latest` (quality, not confirmed), OpenRouter `minimax/minimax-m3.1-flash:free`. Override per tier with `<PROVIDER>_MODEL_QUALITY` / `_FAST`.
* Opt-in live-service tests (`tests/live`, `RUN_LIVE_TESTS=1`).
* Repository consistency tests: no inline code in templates, static assets and document links exist, `.env.example` lists the provider keys.
* Verified migrations (up/down/up, no drift) on a fresh PostgreSQL database, a real Gunicorn smoke test, a dependency audit and a secret scan; results in `FINAL_VERIFICATION.md`.

## Phase 12 - Legal pages and documentation
* `docs/legal/`: privacy, cookie, terms, acceptable use, AI usage and limitations, retention, security
  disclosure, contact. They are templates with `[OPERATOR: ...]` placeholders and make no compliance
  claims; they need legal review.
* Public `/legal` pages render those files (placeholders highlighted) and are linked from the footer.
* New or rewritten docs: agent workflow, HTTP routes, testing, deployment, backup and restore,
  troubleshooting, contributing, changelog, roadmap, security, architecture, schema.

## Phase 11 - Settings and privacy
Preferences, report defaults, opt-in finish emails, ZIP data export, scheduled account deletion with
grace period and `flask purge-accounts`.

## Phase 10 - Quality scoring and exports
Deterministic quality rubric and export gates; Markdown, HTML, Word and PDF exports with a visible
notice on unverified reports.

## Phase 9 - Source explorer and report editor
Source explorer, section editor with optimistic locking, revisions, model rewrite proposals.

## Phase 8 - App shell
Sidebar shell, saved reports, run history, dashboard from real data.

## Phase 7 - Background jobs
Thread, Celery and manual runners; atomic job claiming, heartbeat and recovery; live progress.

## Phase 6 - Agents
LangGraph workflow: plan, research, analyze, write, critic/revise, validate, finalize.

## Phase 5 - Research tools
Tavily search, SSRF-safe fetcher, HTML/PDF/DOCX/TXT extraction, uploads, untrusted-content helpers.

## Phase 4 - Data model
Projects, jobs, sources, excerpts, claims, reports, revisions, evaluations, usage and audit tables.

## Phase 3 - Provider layer
Gemini, Groq, OpenRouter adapters with key rotation, cooldowns, fallback and structured output.

## Phases 0-2 - Foundation and accounts
Flask app factory, validated configuration, PostgreSQL and migrations, registration, email
verification, login, password reset, CSRF, rate limits, CSP, redacted logs.
