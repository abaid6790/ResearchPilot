# Final verification (Phase 13)

This maps the specification's acceptance criteria to what was actually checked in the build
environment (Linux, Python 3.13, PostgreSQL 16) and what was **not**. "Checked" means a command was run
and its result observed.

## Checks run

| Check | Result |
|---|---|
| Automated suite on PostgreSQL | 507 passed, 7 opt-in live tests skipped |
| Automated suite on SQLite | 506 passed, 8 skipped (7 live tests, 1 PostgreSQL-only concurrency test) |
| `ruff check` / `ruff format --check` / byte-compile | clean |
| Migrations on a fresh PostgreSQL database | upgrade (5 revisions), downgrade to base, upgrade again, `db check` reports no drift; 18 tables |
| Dependency audit (`pip-audit` on the installed environment) | no known vulnerabilities at the time of the run; re-run before each release |
| Secret scan of the repository | only obviously fake keys in tests that exercise log redaction; none in source, templates, JavaScript or `.env.example` |
| Inline script / style / event-handler scan of page templates | none (HTML email templates use inline styles, as email clients require) |
| Referenced static assets and document links | all exist (enforced by tests) |
| Real Gunicorn (gthread) start, sign-in, 15 pages | all 200 (404 for an unknown path), CSP and other headers present, CSRF-less login rejected with 400, no errors in the log |
| Production start with weak configuration | refuses to start and lists every problem |

## Acceptance criteria

| Criterion | Status |
|---|---|
| Starts with documented configuration | Checked |
| PostgreSQL primary database; migrations on a fresh database | Checked |
| Email verification and password reset use real SMTP | Implemented; verified against a local SMTP server, not a third-party provider |
| Three provider adapters; four Gemini keys configurable independently | Implemented and tested against fake servers; **not run against the live services** (no keys here) |
| Gemini quota handling and cross-provider fallback tested | Tested with mocks |
| Multi-agent workflow executes tasks; sources and metadata persisted; citations map to source records | Tested with scripted models and fake web/search servers; **no real-model run was made** |
| Critic revisions respect limits; long jobs outside request handlers | Tested |
| View, edit, save and export reports (PDF, DOCX, Markdown, HTML) | Tested; PDF inspected visually |
| Ownership enforced in the backend | Tested (each loader against a second user) |
| Legal and technical documentation | Written; legal text is a template requiring operator input and legal review |
| Separate HTML, CSS, JS per feature; assets exist | Checked |
| No secrets committed or exposed | Checked (scan above); logs redact keys |
| No fake research results or simulated success messages | Dashboard and counts come from database rows; empty states are honest |
| Tests, linting, configuration checks run | Checked |
| Remaining limitations documented | See below |

## Remaining limitations and manual steps
* Live-service behaviour is unverified: run `flask llm-ping`, `flask search-ping`, the opt-in
  `RUN_LIVE_TESTS=1 pytest tests/live`, and one real project, then read the report critically.
* Report quality is not measured against human-reviewed samples; quality scores are automated rules.
* `robots.txt` is not consulted; JavaScript-rendered pages and scanned PDFs yield little or no text.
* PDF export cannot draw Arabic/Urdu script; Word and HTML can.
* No multi-factor authentication, no billing, no Dockerfile.
* `flask purge-accounts` and `flask recover-jobs` must be scheduled by the operator.
* The Windows PowerShell commands were not executed in the build environment (Linux only).
* Browser checks (390 px, dark mode, no console/CSP errors) were done in Chromium for earlier
  phases; the legal pages were checked through HTTP requests and tests, not visually.
* The legal documents are not legal advice and make no compliance claims.
