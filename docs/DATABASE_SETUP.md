# Database setup

PostgreSQL is the primary database. SQLite is used only by the automated tests when
`TEST_DATABASE_URL` is not set.

## Create database and user

See [INSTALLATION.md](INSTALLATION.md#1-postgresql). Then set `DATABASE_URL` in `.env`.

## Migrations

Schema changes are managed with Flask-Migrate (Alembic). The `migrations/` folder is committed.

```bash
flask --app app db upgrade                      # apply all migrations
flask --app app db migrate -m "describe change" # generate after editing models, then review the file
flask --app app db downgrade                    # undo the last migration
flask --app app db current                      # show applied revision
```

Always read a generated migration before applying it. Autogenerate can miss renames.

## Current schema

**`users`**: `id`, `email` (unique, lowercase enforced by a check constraint), `password_hash`,
`display_name`, `email_verified_at`, `is_enabled`, `auth_version`, `last_login_at`, `created_at`,
`updated_at`.

**`auth_tokens`**: `id`, `user_id` (FK to `users`, `ON DELETE CASCADE`, indexed), `purpose`
(`verify_email` or `reset_password`, check constraint), `token_hash` (unique SHA-256 hex digest;
the raw token is never stored), `expires_at`, `used_at`, `created_at`.

`auth_version` is incremented when a password changes; sessions carrying an older value are
rejected, which signs out every other device.

The research, report, quality, usage and audit tables are described in
[DATABASE_SCHEMA.md](DATABASE_SCHEMA.md). SQLite enforces foreign keys here because the app turns
on `PRAGMA foreign_keys` for every SQLite connection; PostgreSQL always does.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `connection refused` | PostgreSQL not running (`sudo pg_ctlcluster 16 main start`, or start the Windows service) |
| `password authentication failed` | Wrong password in `DATABASE_URL`, or special characters not URL-encoded |
| `database "researchpilot" does not exist` | Run the `CREATE DATABASE` statements |
| `permission denied for schema public` | PostgreSQL 15+: create the database with `OWNER researchpilot` |
| `DATABASE_URL must be a PostgreSQL URL` | URL should start with `postgresql+psycopg://` |
| Tests fail with leftover tables | The test database is dropped and recreated per test; use a dedicated `researchpilot_test` |

Backups and restore procedures will be documented with deployment.
