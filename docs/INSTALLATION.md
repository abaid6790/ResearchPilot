# Installation

Requires **Python 3.11 or newer** and **PostgreSQL 14 or newer**. Redis is only needed from
Phase 7 (background jobs) onward.

> Verified on Linux (Python 3.13, PostgreSQL 16). The Windows commands below use standard
> PowerShell and the official installers but were not executed in the build environment.

## 1. PostgreSQL

### Linux (Debian/Ubuntu)

```bash
sudo apt install postgresql
sudo -u postgres psql
```

### Windows

Install PostgreSQL from <https://www.postgresql.org/download/windows/> (remember the
`postgres` superuser password), then open **SQL Shell (psql)** from the Start menu.

### Create the database (both platforms, inside psql)

```sql
CREATE USER researchpilot WITH PASSWORD 'choose-a-strong-password';
CREATE DATABASE researchpilot OWNER researchpilot;
CREATE DATABASE researchpilot_test OWNER researchpilot;   -- optional, for the test suite
```

## 2. Project setup

### Windows PowerShell

```powershell
cd researchpilot-ai
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-dev.txt
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
notepad .env
```

### Linux / macOS

```bash
cd researchpilot-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
nano .env
```

In `.env`, set at least `SECRET_KEY` (the value printed above), `DATABASE_URL`
(`postgresql+psycopg://researchpilot:YOUR_PASSWORD@localhost:5432/researchpilot`) and the
`MAIL_*` values. See [ENVIRONMENT_SETUP.md](ENVIRONMENT_SETUP.md).

A password containing `@`, `:` or `/` must be URL-encoded inside `DATABASE_URL`.

## 3. Email for local development

The app sends real SMTP mail, so give it a server. Easiest: **Mailpit**
(<https://mailpit.axllent.org>), a local SMTP server with a web inbox.

```
MAIL_SERVER=localhost
MAIL_PORT=1025
MAIL_USE_TLS=false
MAIL_DEFAULT_SENDER=ResearchPilot AI <no-reply@localhost>
```

Run Mailpit, then read verification and reset emails at <http://localhost:8025>.
For a real provider (Gmail app password, SendGrid, etc.) use the host/port they document,
`MAIL_USE_TLS=true` with port 587, plus `MAIL_USERNAME` / `MAIL_PASSWORD`.

## 4. Create tables and run

```bash
flask --app app db upgrade
flask --app app run --debug
```

PowerShell is identical. Open <http://localhost:5000>, create an account, open the email,
confirm, sign in.

If the app refuses to start, it prints every configuration problem at once with a fix hint.
`python scripts/check_env.py` gives a fuller diagnosis.

## 5. Tests and quality checks

```bash
python -m pytest -q                              # uses TEST_DATABASE_URL, else in-memory SQLite
python -m pytest --cov=. --cov-report=term-missing
ruff check . && ruff format --check .
```

Set `TEST_DATABASE_URL` (see ENVIRONMENT_SETUP) to run against PostgreSQL. Tests never send real
email or call external APIs: SMTP tests use a throwaway local server.

## 6. Production

`gunicorn wsgi:app -b 127.0.0.1:8000 --workers 3` behind a reverse proxy that terminates HTTPS.
Set `FLASK_ENV=production`, an `https://` `APP_BASE_URL`, `TRUSTED_PROXY_COUNT`, and a Redis
`RATELIMIT_STORAGE_URI`. The app refuses to start in production without these. Gunicorn does not
run on native Windows; use WSL2 or a Linux host. A full deployment guide comes in a later phase.
