"""``flask recover-jobs``: restart jobs whose worker died."""

from __future__ import annotations

import click


def register_cli(app) -> None:
    @app.cli.command("recover-jobs")
    def recover_jobs() -> None:
        """Re-queue active jobs that nobody is running (stale heartbeat). Safe to run repeatedly or from cron."""
        from jobs import enqueue, stale_active_jobs

        jobs = stale_active_jobs(app)
        for job in jobs:
            enqueue(app, job)
            click.echo(f"Re-queued {job.public_id} (status {job.status})")
        click.echo(f"{len(jobs)} job(s) re-queued.")
