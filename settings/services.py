"""Account data export, account deletion (request, cancel, purge) and finish notifications."""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import timedelta

from flask import current_app
from sqlalchemy import inspect, select

from auth.models import User
from core.audit import AuditEvent, record_event
from core.db_utils import utcnow
from extensions import db
from providers.usage import ProviderUsage
from reports.models import QualityEvaluation, ReportDocument, ReportRevision, ReportSection
from reports.render import to_plain_text
from research import constants as c
from research.models import (
    ClaimSource,
    EvidenceExcerpt,
    ResearchClaim,
    ResearchJob,
    ResearchJobEvent,
    ResearchProject,
    ResearchSource,
    ResearchTask,
    ResearchUpload,
)

logger = logging.getLogger(__name__)

DELETE_PHRASE = "DELETE MY ACCOUNT"
_USER_EXCLUDE = {"password_hash", "auth_version"}


# ------------------------------------------------------------------------------ export
def _row(obj, exclude=()) -> dict:
    return {
        col.key: getattr(obj, col.key) for col in inspect(obj).mapper.column_attrs if col.key not in exclude
    }


def _rows(model, *where, order=None) -> list[dict]:
    stmt = select(model).where(*where)
    stmt = stmt.order_by(order if order is not None else model.id)
    return [_row(o) for o in db.session.scalars(stmt)]


def collect_user_data(user: User) -> dict:
    """Everything the application stores about this user, as plain data."""
    projects = []
    for project in db.session.scalars(
        select(ResearchProject).where(ResearchProject.user_id == user.id).order_by(ResearchProject.id)
    ):
        jobs = []
        for job in db.session.scalars(
            select(ResearchJob).where(ResearchJob.project_id == project.id).order_by(ResearchJob.id)
        ):
            claims = []
            for claim in db.session.scalars(
                select(ResearchClaim).where(ResearchClaim.job_id == job.id).order_by(ResearchClaim.id)
            ):
                claims.append(
                    {**_row(claim), "sources": _rows(ClaimSource, ClaimSource.claim_id == claim.id)}
                )
            sources = []
            for src in db.session.scalars(
                select(ResearchSource).where(ResearchSource.job_id == job.id).order_by(ResearchSource.id)
            ):
                sources.append(
                    {**_row(src), "excerpts": _rows(EvidenceExcerpt, EvidenceExcerpt.source_id == src.id)}
                )
            jobs.append({
                **_row(job), "tasks": _rows(ResearchTask, ResearchTask.job_id == job.id),
                "events": _rows(ResearchJobEvent, ResearchJobEvent.job_id == job.id),
                "sources": sources, "claims": claims,
            })  # fmt: skip
        reports = []
        for report in db.session.scalars(
            select(ReportDocument).where(ReportDocument.project_id == project.id).order_by(ReportDocument.id)
        ):
            reports.append({
                **_row(report),
                "sections": _rows(ReportSection, ReportSection.report_id == report.id, order=ReportSection.position),
                "revisions": _rows(ReportRevision, ReportRevision.report_id == report.id),
                "quality_evaluations": _rows(QualityEvaluation, QualityEvaluation.report_id == report.id),
            })  # fmt: skip
        uploads = _rows(ResearchUpload, ResearchUpload.project_id == project.id)
        projects.append({**_row(project), "uploads": uploads, "jobs": jobs, "reports": reports})
    return {
        "exported_at": utcnow(),
        "account": _row(user, _USER_EXCLUDE),
        "projects": projects,
        "provider_usage": _rows(ProviderUsage, ProviderUsage.user_id == user.id),
        "audit_events": _rows(AuditEvent, AuditEvent.actor_user_id == user.id),
    }


_README = """ResearchPilot AI - export of your data
=======================================

data.json   Your account details, preferences, projects (with settings, uploaded document text),
            runs (tasks, progress events, sources, excerpts, claims and their sources), reports
            (sections, revisions, quality evaluations), provider usage records, and audit events
            that name you as the actor.
reports/    Each report as readable text, citations shown as [n] with a reference list.

Not included: your password hash and session data (never exported), other people's data, server
logs, and backups held by the operator. Dates are UTC. Provider usage records contain counts,
models and timings, not the text of your requests.
"""


def build_export(user: User) -> bytes:
    data = collect_user_data(user)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", _README)
        z.writestr("data.json", json.dumps(data, indent=2, default=str, ensure_ascii=False))
        for project in db.session.scalars(select(ResearchProject).where(ResearchProject.user_id == user.id)):
            for report in db.session.scalars(
                select(ReportDocument).where(ReportDocument.project_id == project.id)
            ):
                slug = re.sub(r"[^A-Za-z0-9]+", "-", report.title).strip("-")[:50] or "report"
                z.writestr(f"reports/{slug}-{report.id}.md", to_plain_text(report, {}))
    return buf.getvalue()


# ------------------------------------------------------------------------------ deletion
def request_deletion(user: User) -> None:
    """Schedule the account for removal and stop its running jobs. The user may cancel until the
    grace period ends."""
    from research.services import request_cancel

    if user.deletion_requested_at is None:
        user.deletion_requested_at = utcnow()
    for job in db.session.scalars(
        select(ResearchJob)
        .join(ResearchProject)
        .where(ResearchProject.user_id == user.id, ResearchJob.status.in_(c.JOB_ACTIVE_STATUSES))
    ):
        request_cancel(job, user.id)
    record_event("account_deletion_requested", actor_user_id=user.id, target_type="user", target_id=user.id)
    db.session.commit()


def cancel_deletion(user: User) -> bool:
    if user.deletion_requested_at is None:
        return False
    user.deletion_requested_at = None
    record_event("account_deletion_cancelled", actor_user_id=user.id, target_type="user", target_id=user.id)
    db.session.commit()
    return True


def deletion_date(user: User, grace_days: int | None = None):
    days = current_app.config["ACCOUNT_DELETION_GRACE_DAYS"] if grace_days is None else grace_days
    return user.deletion_requested_at + timedelta(days=days) if user.deletion_requested_at else None


def purge_due_accounts(*, grace_days: int | None = None, now=None, dry_run: bool = False) -> list[int]:
    """Delete accounts whose grace period has ended. Returns the ids that were (or would be) removed.

    The database removes the user's research, reports, sources and tokens (ON DELETE CASCADE).
    Provider usage records and audit events are kept but no longer point at the user."""
    days = current_app.config["ACCOUNT_DELETION_GRACE_DAYS"] if grace_days is None else grace_days
    cutoff = (now or utcnow()) - timedelta(days=days)
    due = list(
        db.session.scalars(
            select(User).where(User.deletion_requested_at.is_not(None), User.deletion_requested_at <= cutoff)
        )
    )
    removed = []
    for user in due:
        removed.append(user.id)
        if dry_run:
            continue
        uid = user.id
        busy = db.session.scalar(
            select(ResearchJob.id)
            .join(ResearchProject)
            .where(ResearchProject.user_id == uid, ResearchJob.status.in_(c.JOB_ACTIVE_STATUSES))
            .limit(1)
        )
        if busy:  # let it stop first; the next purge picks the account up
            removed.pop()
            continue
        db.session.delete(user)
        record_event("account_deleted", target_type="user", target_id=uid)
        db.session.commit()
    return removed


# ------------------------------------------------------------------------------ notifications
def notify_job_finished(app, public_id: str) -> bool:
    """Email the owner when a run ends, if they opted in. Never raises; returns True if an email was sent."""
    from core.mailer import send_email
    from core.urls import external_url

    with app.app_context():
        try:
            job = db.session.scalar(select(ResearchJob).where(ResearchJob.public_id == public_id))
            if job is None or job.is_active:
                return False
            project = db.session.get(ResearchProject, job.project_id)
            user = db.session.get(User, project.user_id)
            if not user or not user.notify_on_finish or not user.is_enabled or user.deletion_pending:
                return False
            with app.test_request_context():
                link = external_url("research.detail", public_id=project.public_id)
                return send_email(
                    to=user.email,
                    subject="Your research run has finished",
                    template="job_finished",
                    title=project.title,
                    status=job.status.replace("_", " "),
                    link=link,
                    display_name=user.display_name,
                )
        except Exception:  # noqa: BLE001 - a notification must never break a run
            logger.exception("Could not send the finish notification for %s", public_id)
            return False
        finally:
            db.session.remove()
