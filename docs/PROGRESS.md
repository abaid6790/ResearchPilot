# Build progress

| Phase | Scope | Status |
|---|---|---|
| 0 | Environment and tooling | Done |
| 1 | Flask foundation | Done |
| 2 | Authentication | Done |
| 3 | LLM provider layer | Done |
| 4 | Database models | Done |
| 5 | Research tools | Done |
| 6 | Agents and LangGraph workflow | Done |
| 7 | Background jobs and live progress | Done |
| 8 | App shell, saved reports, history, dashboard | Done |
| 9 | Source explorer and report editor | Done |
| 10 | Quality scoring and exports | Done |
| 11 | Settings and privacy | Done |
| 12 | Legal pages and documentation | Done (legal text is a template needing operator input and legal review) |
| 13 | Hardening and final verification | Done (see FINAL_VERIFICATION.md) |

## Verified in the build environment

- 507 tests pass on PostgreSQL 16 (7 opt-in live tests skipped); 506 pass on SQLite (8 skipped: the 7 live tests and one PostgreSQL-only concurrency test); `ruff check` clean
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
- Phase 7 adds the Start/Stop/Resume buttons, background running and live progress. Report editing, source explorer, quality scores and exports are later phases; the report page is read-only.
- Phase 6 agents were tested with scripted models and fake web/search servers; real model quality and prompt tuning are unverified. See `docs/AGENTS.md`.
- Phase 7 was exercised with scripted models; a long real run is unverified. See `docs/BACKGROUND_JOBS.md` for the Gunicorn thread setting streaming needs.
- Phase 8 adds navigation and views only. Dashboard activity comes from real job events and usage from recorded provider calls (current UTC month); nothing is estimated. Tokens show only where a provider reported them.
- Phase 9: after any edit a report is no longer marked "checked" (`export_ready` is cleared) because the automatic checks ran on the earlier text. Re-run the checks on the Quality page to update the mark. Editing is paused while the run that wrote the report is active. Autosave keeps one rolling history entry; a stale version is refused (409) rather than overwritten. Rewrite suggestions are stored as `proposed` revisions and never change the text until applied; a suggestion made before a later edit is refused as stale.
- Phase 9 critique and rewrite calls use the same provider layer and were exercised with scripted models only.
- Phase 10: exports are never blocked. A report that has not passed its gates is exported with a visible "NOT FULLY VERIFIED" notice and its warnings. The PDF uses DejaVu Serif when installed (else Helvetica) and cannot draw scripts such as Arabic/Urdu (they are replaced by "?" with a note in the file); Word and HTML have no such limit. Word citations link to the reference list, but the contents list has no page numbers.
- Phase 11: account deletion is a scheduled removal (default 7 days, cancel any time) carried out by `flask purge-accounts`; run it from cron. Nothing removes accounts automatically unless you schedule it. Data export is generated in memory on request, so very large accounts may be slow. Legal pages and policies are Phase 12.
