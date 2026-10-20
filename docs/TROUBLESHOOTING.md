# Troubleshooting

First run `python scripts/check_env.py`. For providers: `flask --app app llm-status`, `llm-ping`,
`search-ping`. For a single page: `flask --app app fetch-check <url>`.

| Symptom | Likely cause | What to do |
|---|---|---|
| App will not start and prints configuration errors | Missing or invalid environment values | Fix every listed item; see ENVIRONMENT_SETUP.md |
| `OperationalError` / connection refused | PostgreSQL not running or wrong `DATABASE_URL` | Start the server; URL-encode special characters in the password |
| Tables missing | Migrations not applied | `flask --app app db upgrade` |
| Start research refused: no provider key | No `GEMINI/GROQ/OPENROUTER_API_KEY_n` | Add a key, restart, `flask llm-ping` |
| Start research refused: no search key | `TAVILY_API_KEY` missing | Add it, `flask search-ping` |
| Run stays "queued" | `JOB_RUNNER=celery` but no worker, or Redis down; or `manual` | Start the worker; check `REDIS_URL`; then `flask recover-jobs` |
| Run stops after a restart | Thread runner dies with the web process | `flask recover-jobs`; use Celery in production |
| Run failed: provider rate limit or quota | Keys cooling down or exhausted | Wait, add keys, `flask llm-status`; Resume from saved progress |
| Run failed: call budget | Run used its call allowance | Resume or reduce sources/rounds in the project settings |
| Few or empty sources | Pages need JavaScript, are blocked/paywalled, or refused by the SSRF guard | `fetch-check <url>`; open the source's warnings in the explorer |
| Live progress does not update | Proxy buffers the stream, or sync Gunicorn workers | Use `--worker-class gthread`; disable proxy buffering; the page falls back to polling |
| "Too many requests" (429) | Rate limit | Wait; production needs Redis storage so limits are shared |
| Everyone shares one rate limit / wrong IPs | `TRUSTED_PROXY_COUNT` wrong | Set it to the number of proxies |
| Verification emails not arriving | SMTP settings or spam folder | Check `MAIL_*`; with Mailpit read <http://localhost:8025>; look in logs for the SMTP error |
| Emailed links point to the wrong host | `APP_BASE_URL` wrong | Set it to the public https URL |
| Signed out every time / CSRF errors behind HTTPS | Production cookie is `Secure`; site served over HTTP | Serve over HTTPS |
| Editor says report changed (409) | Another tab saved a newer version | Reload and re-apply your change |
| Report not marked checked after editing | Edits clear the mark | Quality page > Re-run checks |
| PDF shows `?` for some text | PDF cannot draw some scripts (Arabic, Urdu) | Export Word or HTML |
| Deleted account still exists | `purge-accounts` not scheduled / grace period / active run | Schedule it; see ACCOUNT_DATA.md |
| Tests fail with connection errors | PostgreSQL not running or `TEST_DATABASE_URL` wrong | Start it, or run with `TEST_DATABASE_URL=` to use SQLite |

If something still fails, check the application log (secrets are masked) and the run's event list on its
page, then see [CONTRIBUTING.md](CONTRIBUTING.md) for how to report a bug without sharing keys or data.
