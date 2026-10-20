# Account data: preferences, export and deletion

## What the application stores about an account
Email, display name, password hash, verification state, preferences (theme, finish notifications, default report settings), and everything the user creates: projects and their settings, text extracted from uploaded documents (the original files are not stored), runs with progress events, sources and excerpts, claims, reports with sections, revisions and quality evaluations. Separately: provider usage records (provider, model, timings, token counts, outcome; no prompt or response text) and audit events (type, time, target id, small details such as a format name).

## Preferences
- **Theme**: stored on the account and applied on every device. The top-bar toggle changes only the current browser (it uses `localStorage` key `rp-theme`) until the user saves a theme in Settings.
- **Finish notifications**: off by default. One email per finished run containing the project title, status and a link built from `APP_BASE_URL`; never report text. Account emails (verification, password reset, password changed) are not optional.
- **Report defaults**: validated by the same `ResearchSettings` rules as a project and used only to prefill the new-project form.

## Data export (Settings > Privacy and data)
A ZIP generated on request (5 per day): `data.json` (account without password hash or session data, projects, uploads text, runs, tasks, events, sources, excerpts, claims and claim sources, reports, sections, revisions, quality evaluations, provider usage, audit events naming the user), `reports/*.md`, `README.txt`. Not included: other people's data, server logs, backups. The file is built in memory.

## Account deletion
1. The user confirms with their password and a typed phrase. The account is marked `deletion_requested_at`, running jobs get a cancel request, new projects and runs are blocked, and a banner shows the date. The user can cancel until then.
2. After `ACCOUNT_DELETION_GRACE_DAYS` (default 7) `flask purge-accounts` deletes the account. Accounts with a still-active run are skipped until it stops. Schedule the command (cron or similar); the application does not do it by itself.
3. The database cascades the delete to: tokens, projects, uploads, runs, tasks, events, sources, excerpts, claims, claim links, reports, sections, revisions, quality evaluations.
4. **Kept, unlinked:** provider usage records and audit events keep their rows but lose the link to the user (`user_id` / `actor_user_id` become NULL). Audit details hold no email address. One `account_deleted` event records the removed user id.
5. **Not changed by the application:** database backups, server and proxy logs, and copies held by external providers (LLM and search services received research text while runs executed). Their retention is the operator's responsibility; see the retention document in Phase 12. Immediate removal from backups is not promised.

Deleting a single project is immediate in the database and removes its runs, sources, claims and reports.
