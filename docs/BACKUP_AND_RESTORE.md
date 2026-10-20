# Backup and restore

All durable data is in PostgreSQL. Redis holds only the job queue, rate-limit counters and key
cooldowns (safe to lose). The application stores no uploaded files on disk and no user files elsewhere.
Environment files (secrets) are not in the database: back them up separately and securely.

## Back up
```bash
pg_dump --format=custom --no-owner --file=researchpilot-$(date +%F).dump "<DATABASE_URL without the +psycopg part>"
```
* Run it daily from cron or your provider's snapshot feature, and copy it off the server.
* Backups contain personal data and research content: encrypt them, restrict access, and set an
  expiry. The retention period is the operator's decision (see `docs/legal/DATA_RETENTION_POLICY.md`).
* Test a restore regularly; an untested backup is not a backup.

## Restore
```bash
createdb researchpilot_restore
pg_restore --no-owner --dbname=researchpilot_restore researchpilot-YYYY-MM-DD.dump
```
Point `DATABASE_URL` at the restored database, run `flask --app app db upgrade` (a no-op if the dump is
current), then start the application and worker.

## After a restore: honour deletions
Accounts and projects deleted after the backup was taken reappear. Before reopening to users:
1. Run `flask --app app purge-accounts`. Accounts that had a recorded deletion request in the backup are
   removed once their grace period has passed; accounts deleted *after* the backup need their own
   record, so keep a log of deletions (the `account_deleted` audit events are in the database but a
   restore rolls them back) outside the database.
2. Re-delete any individually deleted projects that are recorded in your log.
3. Run `flask --app app recover-jobs` to settle runs that were in progress when the backup was taken.

## What backups mean for deletion
Deleting data in the application does not remove it from earlier backups; it disappears when those
backups expire. Do not promise users immediate deletion from backups.

## Disaster checklist
Database restored; secrets restored; `SECRET_KEY` unchanged (changing it signs everyone out);
migrations current; `scripts/check_env.py` passes; `/health` is ok; one test project runs.
