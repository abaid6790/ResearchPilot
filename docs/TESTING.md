# Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q                              # in-memory SQLite unless TEST_DATABASE_URL is set
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/researchpilot_test python -m pytest -q
python -m pytest --cov=. --cov-report=term-missing
ruff check . && ruff format --check .
```

Use a **separate** database for `TEST_DATABASE_URL`; the fixtures create and drop tables. A full run
takes a few minutes; run it in the background and read the log when you are on a slow machine.
Both SQLite and PostgreSQL must pass. PostgreSQL-only behaviour (partial unique indexes under
concurrency) is marked with a skip on SQLite.

## What the tests use
* `tests/conftest.py`: `app`, `client`, `outbox` fixtures and helpers `make_user`, `login`, `post_form`,
  `get_csrf`.
* `tests/fake_web.py`: local HTTP servers standing in for Tavily and web pages.
* `tests/fake_llm.py`: scripted models (`FakeLLM`), including forced failures.
* Tests never call real providers, send real email or reach the internet. Tests marked `live` are
  opt-in and need real keys (`pytest -m live`).

## Layout
| File group | Covers |
|---|---|
| `test_auth*`, `test_config*`, `test_security*` | accounts, tokens, CSRF, CSP, headers, config validation |
| `test_models*` | schema constraints, ownership (every loader is checked against a second user) |
| provider tests | adapters, key rotation, fallback, structured output |
| research-tool tests | SSRF guard, fetcher, extraction, uploads |
| agent and job tests | workflow, validation, runners, claiming, SSE |
| `test_phase8_shell.py` | shell, saved reports, history, dashboard |
| `test_phase9_editor.py` | editor, revisions, optimistic locking, source explorer |
| `test_phase10_quality_exports.py` | scoring, gates, MD/HTML/DOCX/PDF exports |
| `test_phase11_settings.py` | preferences, notifications, data export, account deletion |
| `test_phase12_docs.py` | legal pages, cookie policy vs. configuration, documentation completeness, no inline code in templates, static assets and document links exist |
| `tests/live/` | opt-in real-service checks (see below) |

## Browser checks
Playwright is in the dev requirements for manual checks (390 px width, dark mode, console and CSP
errors). The CSP forbids `unsafe-eval`, so `page.wait_for_function` does not work; use
`wait_for_timeout`. Install browsers with `playwright install chromium`.

## Live (opt-in) tests
```bash
RUN_LIVE_TESTS=1 python -m pytest tests/live -m live -v
```
`tests/live/` calls the real services with the keys in your environment or `.env`: one tiny request per
configured LLM key, a router call, one Tavily search, and the safe fetcher against a public page (plus
a check that it refuses the cloud-metadata address). Each check is skipped when its key is missing.
They use real quota and need internet access; the routine suite never runs them. Note the fetcher
check fails on a network whose DNS answers with private addresses (such as some sandboxes or
corporate proxies), because the SSRF guard correctly refuses those.

## What is not covered
The routine suite does not exercise real Gemini, Groq, OpenRouter, Tavily or arbitrary live websites, and
generated report quality is not measured by tests. Before relying on a deployment run
`flask llm-ping`, `flask search-ping`, then one real project and read the result critically.
