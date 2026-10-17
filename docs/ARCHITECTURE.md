# Architecture (Phases 0-4)

## Layout

```
app.py            create_app(): config, validation, extensions, blueprints, errors, headers
config.py         environment-driven config classes + validate_config()
extensions.py     db, migrate, login_manager, csrf, limiter (naming convention for Alembic)
core/             logging redaction, security headers, error handlers, SMTP mailer, health, URL helper
auth/             models, forms, services (business logic), tokens, routes, decorators
research_tools/    Tavily search, safe fetching (SSRF guard), HTML/PDF/DOCX/TXT extraction, untrusted-content helpers, scoring
providers/        LLM layer: adapters (gemini, groq, openrouter), router, key_manager, models, structured
research/         projects, jobs, tasks, sources, claims: models, settings schema, services, ownership loaders
reports/          report documents, sections, revisions, quality evaluations
dashboard/        landing + dashboard (verified users only)
settings/         profile and password change
templates/ static/ tests/ migrations/ scripts/ docs/
```

Routes stay thin; business rules live in `auth/services.py` so they are testable without HTTP.

## Account flows

```mermaid
sequenceDiagram
    participant U as User
    participant A as App
    participant M as SMTP
    U->>A: POST /auth/register
    A->>A: create user (unverified), hash token
    A-->>M: verification email (background thread)
    A-->>U: same message whether or not the email already existed
    U->>A: GET /auth/verify/TOKEN (shows button, consumes nothing)
    U->>A: POST /auth/verify/TOKEN (CSRF)
    A->>A: atomically mark token used, set email_verified_at
```

## Security decisions

- **No account enumeration.** Registration and "forgot password" respond identically for known
  and unknown addresses; unknown-user login burns equal hashing time; email is sent in a
  background thread so response time does not leak existence.
- **Tokens.** 256-bit random, only the SHA-256 digest is stored, single-use via an atomic
  `UPDATE ... WHERE used_at IS NULL`, newer tokens supersede older ones, per-account cooldown.
- **Emailed links use `APP_BASE_URL`, never the `Host` header**, preventing password-reset
  poisoning (covered by a test that fails if the fix is removed).
- **Link scanners.** Email links open a confirmation page; the token is consumed only on POST.
- **Sessions.** HttpOnly, SameSite=Lax, Secure in production, cleared on login and logout,
  invalidated everywhere on password change via `auth_version`.
- **CSRF** on every state-changing request. Logout is POST-only.
- **CSP** is `default-src 'self'` with no inline scripts or styles, enforced by a test that
  scans every page. Security headers set on all responses; HTML responses are `no-store`.
- **Open redirects** blocked: `next` must be a same-site relative path.
- **Rate limits** on register, login, reset, resend and password change (per IP). In-memory
  storage is per process; production requires Redis.
- **Logs** pass through a redaction filter (provider key formats, bearer tokens, token URLs,
  connection-string passwords, and the exact values of configured secrets).

## Not yet built

Research tools, agents/LangGraph workflow, research UI,
exports, background jobs, quality scoring logic, legal documents, deployment guide.

## LLM provider layer (Phase 3)

Application code calls `get_router().generate(LLMRequest(...))` and never talks to a provider
directly. Adapters translate one request into one provider's HTTP API and classify every
failure into an `ErrorKind`; the router decides what to do about each kind (rotate key, retry,
fall back, or give up) and the key manager remembers which keys are cooling down. Structured
output is always validated locally with Pydantic, whatever the provider enforces natively.
Full behaviour: `docs/API_KEY_ROTATION.md`.
