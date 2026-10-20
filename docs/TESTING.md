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
| `test_phase12_docs.py` | legal pages, cookie policy vs. configuration, documentation completeness |

## Browser checks
Playwright is in the dev requirements for manual checks (390 px width, dark mode, console and CSP
errors). The CSP forbids `unsafe-eval`, so `page.wait_for_function` does not work; use
`wait_for_timeout`. Install browsers with `playwright install chromium`.

## What is not covered
No automated test exercises real Gemini, Groq, OpenRouter, Tavily or arbitrary live websites, and
generated report quality is not measured by tests. Before relying on a deployment run
`flask llm-ping`, `flask search-ping`, then one real project and read the result critically.
