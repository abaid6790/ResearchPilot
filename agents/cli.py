"""Operator commands to run a research job without the web UI or a worker (Phase 7 adds those)."""

from __future__ import annotations

import click
from sqlalchemy import select


def register_cli(app) -> None:
    @app.cli.command("run-research")
    @click.argument("project_id")
    @click.option(
        "--resume", is_flag=True, help="Continue the project's unfinished job instead of starting a new one."
    )
    @click.option("--show", is_flag=True, help="Print the finished report as Markdown.")
    def run_research(project_id: str, resume: bool, show: bool) -> None:
        """Run a research job for PROJECT_ID (the id in the project page URL) and wait for it to finish.

        Uses real API keys and spends quota: run `flask llm-ping` and `flask search-ping` first.
        """
        from agents.runner import default_context, run_job
        from extensions import db
        from research.models import ResearchProject
        from research.services import ActiveJobExistsError, create_job

        project = db.session.scalar(select(ResearchProject).where(ResearchProject.public_id == project_id))
        if project is None:
            click.echo("No project with that id.")
            raise SystemExit(1)
        try:
            job = create_job(project)
        except ActiveJobExistsError:
            job = next((j for j in project.jobs if j.is_active), None)
            if not resume or job is None:
                click.echo("This project already has an unfinished job. Use --resume to continue it.")
                raise SystemExit(1) from None
        click.echo(f"Running job {job.public_id} ...")
        run_job(default_context(app, job.id))
        db.session.refresh(job)
        for event in job.events:
            click.echo(f"  [{event.seq:>3}] {event.level:<7} {event.message}")
        click.echo(
            f"\nStatus: {job.status}"
            + (f"  ({job.error_code}: {job.error_message})" if job.error_code else "")
        )
        for issue in job.unresolved_issues or []:
            click.echo(f"  ! {issue['severity']}: {issue['detail']}")
        if show:
            _print_report(job)
        raise SystemExit(0 if job.status.startswith("completed") else 1)

    @app.cli.command("show-report")
    @click.argument("job_id")
    def show_report(job_id: str) -> None:
        """Print the report of a job (by job id) as Markdown."""
        from extensions import db
        from research.models import ResearchJob

        job = db.session.scalar(select(ResearchJob).where(ResearchJob.public_id == job_id))
        if job is None:
            click.echo("No job with that id.")
            raise SystemExit(1)
        _print_report(job)


def _print_report(job) -> None:
    from extensions import db
    from reports.models import ReportDocument
    from reports.render import to_markdown

    report = db.session.scalar(select(ReportDocument).where(ReportDocument.job_id == job.id))
    click.echo(to_markdown(report) if report else "This job has no report.")
