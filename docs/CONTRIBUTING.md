# Contributing

## Set up
Follow [INSTALLATION.md](INSTALLATION.md), then `pip install -r requirements-dev.txt`. Use a separate
test database ([TESTING.md](TESTING.md)).

## Rules the code base keeps
* **No secrets in the repository.** API keys come from the environment only; never in Python,
  templates, JavaScript, fixtures, docs or logs. Do not commit `.env`.
* **Strict CSP.** No inline `<script>`, inline `style=` attributes or inline event handlers; each
  feature has its own template, CSS and JS file under `static/`. A test scans pages for violations.
* **Untrusted content.** Web pages, uploads and model output are data. Validate model output against
  a schema; never give a model a tool that acts.
* **Ownership.** Load project data through `research/access.py` so foreign ids return 404.
* **Honest UI and docs.** Show real data or an honest empty state; no invented numbers, sources, URLs,
  quotes, dates, company details, certifications or compliance claims.
* **Migrations** for every model change (`flask --app app db migrate -m "..."`), reviewed by hand, and
  checked on PostgreSQL both upgrading and downgrading.

## Workflow
1. Make a focused change with tests.
2. `ruff check . && ruff format --check .`
3. Run the full suite on PostgreSQL **and** SQLite; read the results before claiming they pass.
4. Update the documentation that the change affects, and add a line to [CHANGELOG.md](CHANGELOG.md).

## Style
Python: type hints where useful, line length 110, small functions, business rules in services rather
than routes. JavaScript: vanilla ES modules-free scripts, no build step. CSS: tokens from `base.css`,
mobile first; check 390 px and dark mode.

## Reporting problems
Include the steps, expected and actual result, and the version. Do not paste keys, passwords, tokens
or other people's data. Security issues: follow `docs/legal/SECURITY_DISCLOSURE.md`, not a public issue.
