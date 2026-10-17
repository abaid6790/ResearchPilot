# Progress

| Phase | Scope | Status |
|---|---|---|
| 0 | Environment, skeleton, requirements, `.env.example`, `scripts/check_env.py` | Done |
| 1 | Flask foundation: config, extensions, factory, templates, errors, health, security headers | Done |
| 2 | Authentication: register, verify email, login, reset, change password, rate limits | Done |
| 3 | LLM provider layer: Gemini/Groq/OpenRouter adapters, key rotation, fallback | Done (not run against live providers) |
| 4 | Remaining database models | Done |
| 4b | Research workspace UI: new-research form, project list, project page, archive, delete, real dashboard counts | Done |
| 5 | Research tools (Tavily, safe page fetching, extraction, PDF/DOCX/TXT reader, uploads) | Done (not run against live Tavily or real websites) |
| 6 | Agents and LangGraph workflow | Planned |
| 7 | Background jobs (Celery/Redis) and live progress | Planned |
| 8 | Core research UI | Planned |
| 9 | Source explorer and report editor | Planned |
| 10 | Quality scoring and exports | Planned |
| 11 | Settings, privacy, polish | Planned |
| 12 | Documentation and legal drafts | Planned |
| 13 | Hardening and final verification | Planned |

## Verified in the build environment

- 227 tests pass on PostgreSQL 16 and on SQLite; `ruff check` clean (94 cover the provider layer, 45 the data model, 28 the research pages)
- Migrations applied from an empty database (base to head), downgraded and re-applied, and checked for drift against the models on PostgreSQL
- Live run: Gunicorn + real SMTP delivery + real browser session (register, confirm, sign in,
  edit profile, sign out); no server errors, no secrets in logs
- Browser check (Chromium): no console or CSP errors, no horizontal overflow at 390px, dark mode,
  theme persistence, password strength meter and show/hide

## Known limitations and manual steps

- Not executed here: the Windows PowerShell commands, a real third-party SMTP provider (only a
  local SMTP server was used), and live calls to Gemini/Groq/OpenRouter/Tavily (no keys were
  available). After adding keys run `flask llm-ping`; see `docs/API_KEY_ROTATION.md`.
- Rate limiting uses in-memory storage unless `RATELIMIT_STORAGE_URI` points at Redis.
- Registering with an existing email sends nothing (to avoid enumeration), so a forgetful user
  must use "Forgot your password?".
- Account deletion, data export and theme preference storage are Phase 11.
- Phase 4 adds tables only. Nothing creates projects or jobs yet (UI in Phase 8, workflow in Phase 6/7), so the dashboard still shows no research. The DB-level rules are tested directly.
- The `settings` JSON limits (max sources, rounds) are defaults chosen here; tune them in `research/schemas.py` after seeing real API usage.
- The project page has no "Start research" button on purpose: runs cannot execute until the research tools and agents (Phases 5-7) exist. Next step is to build those so a run works end to end.
- Phase 5 adds the tools but nothing calls them yet: the agents (Phase 6) and background jobs (Phase 7) do. Uploaded documents are read and stored with the project; they become sources when a run starts.
- Phase 5 was tested against local fake servers (Tavily and websites), not the live services. Run `flask search-ping` with your key and `flask fetch-check <url>` on a few real pages first.
- Page fetching does not check robots.txt, does not run JavaScript, and cannot read scanned PDFs (no OCR).
