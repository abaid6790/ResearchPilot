"""``flask purge-accounts``: remove accounts whose deletion grace period has ended."""

from __future__ import annotations

import click


def register_cli(app) -> None:
    @app.cli.command("purge-accounts")
    @click.option("--dry-run", is_flag=True, help="List the accounts that would be removed, change nothing.")
    @click.option("--grace-days", type=int, default=None, help="Override ACCOUNT_DELETION_GRACE_DAYS.")
    def purge_accounts(dry_run: bool, grace_days: int | None) -> None:
        """Delete accounts whose deletion request is older than the grace period. Safe to run from cron."""
        from settings.services import purge_due_accounts

        ids = purge_due_accounts(grace_days=grace_days, dry_run=dry_run)
        verb = "Would remove" if dry_run else "Removed"
        click.echo(f"{verb} {len(ids)} account(s)." + (f" Ids: {', '.join(map(str, ids))}" if ids else ""))
