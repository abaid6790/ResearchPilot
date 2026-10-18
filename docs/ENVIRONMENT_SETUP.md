# Environment variables

All settings are read from the environment, optionally from a `.env` file in the project root.
An empty value counts as unset. `.env` is git-ignored; never commit it.

## Core

| Variable | Required | Default | Notes |
|---|---|---|---|
| `FLASK_ENV` | no | `development` | `development`, `testing` or `production` |
| `SECRET_KEY` | **yes** | none | 32+ characters. Generate: `python -c "import secrets; print(secrets.token_hex(32))"`. Changing it signs everyone out. |
| `APP_BASE_URL` | no | `http://localhost:5000` | Used to build links in emails (never the request `Host` header). Must be `https://` in production. |
| `DATABASE_URL` | **yes** | none | `postgresql+psycopg://USER:PASSWORD@HOST:PORT/DB` |
| `TEST_DATABASE_URL` | no | in-memory SQLite | Used only by the test suite. Tests drop and recreate tables, so never point it at real data. |

## Email (SMTP)

| Variable | Default | Notes |
|---|---|---|
| `MAIL_SERVER` | none | Required in production |
| `MAIL_PORT` | `587` | |
| `MAIL_USE_TLS` | `true` | STARTTLS. Set `false` for Mailpit/local servers. |
| `MAIL_USE_SSL` | `false` | Implicit TLS (usually port 465). Cannot combine with `MAIL_USE_TLS`. |
| `MAIL_USERNAME`, `MAIL_PASSWORD` | none | Sent only if both are set |
| `MAIL_DEFAULT_SENDER` | none | e.g. `ResearchPilot AI <no-reply@yourdomain>`. Required in production. |
| `MAIL_TIMEOUT_SECONDS` | `15` | Hard limit per SMTP connection |

Without SMTP settings the app still starts (with a warning) but cannot send verification or
reset emails, and says so honestly when you ask for one.

## Authentication tuning

| Variable | Default |
|---|---|
| `EMAIL_VERIFY_TTL_HOURS` | `24` |
| `PASSWORD_RESET_TTL_MINUTES` | `60` |
| `SESSION_LIFETIME_HOURS` | `12` |
| `MAX_CONTENT_LENGTH_MB` | `16` |

## Deployment

| Variable | Default | Notes |
|---|---|---|
| `RATELIMIT_STORAGE_URI` | `memory://` | Use Redis in production (`redis://localhost:6379/1`) so limits are shared by workers |
| `TRUSTED_PROXY_COUNT` | `0` | Number of reverse proxies in front of the app, so client IPs are correct for rate limiting |
| `REDIS_URL` | none | Background jobs, from Phase 7 |

## LLM providers (Phase 3)

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY_1..8`, `GROQ_API_KEY_1..8`, `OPENROUTER_API_KEY_1..8` | none | API keys. Blank or `changeme` values and duplicates are ignored |
| `LLM_PROVIDER_ORDER` | `gemini,groq,openrouter` | Fallback order; omit a provider to disable it |
| `GEMINI_API_KEY_n_PROJECT` | one project per key | Label keys that belong to the same Google Cloud project (they share quota) |
| `<PROVIDER>_MODEL_QUALITY` / `_FAST` | see `providers/models.py` | Model per tier |
| `LLM_REQUEST_TIMEOUT_S` | `60` | Per-call timeout |
| `LLM_OVERALL_DEADLINE_S` | `180` | Ceiling for one request including retries and fallbacks |
| `LLM_MAX_TOTAL_ATTEMPTS` | `8` | Provider calls per request |
| `LLM_MAX_RETRIES_PER_KEY` | `2` | Extra tries on one key after timeouts/5xx/malformed replies |
| `LLM_MAX_VALIDATION_RETRIES` | `1` | "Fix your JSON" retries for structured output |
| `LLM_MAX_CONCURRENCY` | `8` | In-flight calls per provider per process |
| `LLM_BACKOFF_BASE_S` / `LLM_BACKOFF_CAP_S` | `0.5` / `8` | Retry backoff (with jitter) |
| `LLM_MAX_INLINE_WAIT_S` | `5` | Longest a request waits for a cooldown to end |
| `LLM_RATE_LIMIT_COOLDOWN_S` | `30` | Cooldown for a 429 that gives no retry time |
| `LLM_AUTH_COOLDOWN_S` | `21600` | How long a rejected key is skipped before a recheck |
| `LLM_HEALTH_STORE` | `memory` | `redis` shares cooldowns between processes (needs `REDIS_URL`) |
| `*_BASE_URL` | official endpoints | Override (tests, proxies) |

Other keys, `TAVILY_API_KEY` and `FIRECRAWL_API_KEY`, are used from Phase 5. Keys are redacted
from logs by exact value and by common key formats, and are never copied into Flask's config.
Several keys do not guarantee extra quota; see `docs/API_KEY_ROTATION.md`.

Commands: `flask llm-status` (no network), `flask llm-ping` (one tiny real call per key; uses a
little quota), `flask llm-reset` (forget cooldowns). `GET /health/providers` (signed-in users)
shows the same status as JSON without calling any provider.

## Web search and fetching (Phase 5)

| Variable | Default | Meaning |
|---|---|---|
| `TAVILY_API_KEY` | empty | Tavily search key (free at tavily.com). Verify with `flask search-ping` (uses about 1 credit) |
| `TAVILY_BASE_URL` | `https://api.tavily.com` | Override for testing |
| `TAVILY_TIMEOUT_S` | 30 | Per-request timeout |
| `FETCH_MAX_BYTES` | 5242880 | Largest page body accepted after decompression (100 KB to 25 MB) |
| `FETCH_TIMEOUT_S` | 30 | Total time per page (5 to 120) |
| `FETCH_MAX_REDIRECTS` | 5 | Redirects followed, each re-checked (0 to 10) |

Try a page with `flask fetch-check <url>`. SSRF protection has no off switch; see `docs/SECURITY.md`.

## Agents (Phase 6)

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_MAX_LLM_CALLS` | 120 | Most model calls one research job may make before it stops |
