# Data Retention Policy

**Operator:** [OPERATOR: legal name]  **Effective date:** [OPERATOR: effective date]

This describes what the software does and leaves the operator's own choices as placeholders. Periods
marked `[OPERATOR: ...]` are decisions the operator must make; the software does not choose them.

| Data | What the application does | Operator period |
|---|---|---|
| Account, projects, uploads' extracted text, runs, sources, excerpts, claims, reports, revisions, quality results | Kept until you delete the project or the account | [OPERATOR: any inactivity or maximum period, or "until deleted"] |
| Deleted project | Removed from the database immediately with its runs, sources, claims and reports | n/a |
| Deleted account | Marked as pending, then removed by the scheduled command `flask purge-accounts` after `ACCOUNT_DELETION_GRACE_DAYS` (default 7 days); the user can cancel before then. Accounts with a running job wait until it stops | [OPERATOR: confirm grace period and that the command is scheduled] |
| Provider usage records and audit events | Rows are kept but unlinked from the user on account deletion | [OPERATOR: period after which they are deleted, or "kept"] |
| Verification and reset tokens | Single-use and time-limited | [OPERATOR: cleanup period if any] |
| Original uploaded files | Not stored; only extracted text is | n/a |
| Data export ZIP | Built in memory when requested and not stored | n/a |
| Application, web-server and proxy logs | Controlled by the operator's hosting setup | [OPERATOR: log retention] |
| Database backups | Controlled by the operator | [OPERATOR: backup retention schedule] |
| Copies at AI, search and email providers | Controlled by each provider's terms | [OPERATOR: check each provider's retention] |

## Backups and external copies
Deleting data in the application does **not** immediately delete it from backups, logs, or copies held
by external providers. Backups expire on the operator's schedule; if a backup is restored, accounts
deleted since must be purged again (run `flask purge-accounts`, and re-apply any recorded deletions).
No immediate deletion from backups is promised.

## Points that need legal review
* Retention limits required or forbidden by law for each category of data.
* Handling of deletion requests that arrive while data exists only in backups.
* Provider retention and deletion terms.
