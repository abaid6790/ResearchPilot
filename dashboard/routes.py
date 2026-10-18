"""Dashboard routes."""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user
from sqlalchemy import func, select

from auth.decorators import verified_required
from extensions import db
from reports.models import ReportDocument
from research.access import projects_of
from research.constants import JOB_ACTIVE_STATUSES
from research.models import ResearchJob, ResearchProject

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    return redirect(url_for("auth.login"))


@bp.get("/dashboard")
@verified_required
def index():
    # Every figure here is counted from the database; nothing is estimated or invented.
    stats = {
        "projects": db.session.scalar(
            select(func.count())
            .select_from(ResearchProject)
            .where(ResearchProject.user_id == current_user.id)
        ),
        "running": db.session.scalar(
            select(func.count())
            .select_from(ResearchJob)
            .join(ResearchProject)
            .where(ResearchProject.user_id == current_user.id, ResearchJob.status.in_(JOB_ACTIVE_STATUSES))
        ),
        "reports": db.session.scalar(
            select(func.count())
            .select_from(ReportDocument)
            .join(ResearchProject)
            .where(ResearchProject.user_id == current_user.id)
        ),
    }
    recent = db.session.scalars(
        projects_of(current_user)
        .where(ResearchProject.archived_at.is_(None))
        .order_by(ResearchProject.created_at.desc(), ResearchProject.id.desc())
        .limit(5)
    ).all()
    activity = _recent_activity()
    usage = _usage_this_month()
    return render_template("dashboard/index.html", stats=stats, recent=recent, activity=activity, usage=usage)


def _recent_activity(limit: int = 8):
    """Latest progress events across the user's runs (only events that mark something happening)."""
    from research.models import ResearchJobEvent

    kinds = (
        "job_queued",
        "job_completed",
        "job_failed",
        "job_cancelled",
        "job_resumed",
        "plan_ready",
        "critic_review",
    )
    return db.session.execute(
        select(ResearchJobEvent, ResearchProject)
        .join(ResearchJob, ResearchJob.id == ResearchJobEvent.job_id)
        .join(ResearchProject, ResearchProject.id == ResearchJob.project_id)
        .where(ResearchProject.user_id == current_user.id, ResearchJobEvent.event_type.in_(kinds))
        .order_by(ResearchJobEvent.created_at.desc(), ResearchJobEvent.id.desc())
        .limit(limit)
    ).all()


def _usage_this_month():
    """Real provider call counts and tokens recorded for this user this calendar month (UTC)."""
    from core.db_utils import utcnow
    from providers.usage import ProviderUsage

    now = utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.session.execute(
        select(
            ProviderUsage.provider,
            func.count(),
            func.sum(
                func.coalesce(ProviderUsage.prompt_tokens, 0)
                + func.coalesce(ProviderUsage.completion_tokens, 0)
            ),
            func.sum(db.case((ProviderUsage.outcome == "ok", 0), else_=1)),
        )
        .where(ProviderUsage.user_id == current_user.id, ProviderUsage.created_at >= start)
        .group_by(ProviderUsage.provider)
        .order_by(ProviderUsage.provider)
    ).all()
    return [
        {"provider": p, "calls": int(c), "tokens": int(t or 0), "failed": int(f or 0)} for p, c, t, f in rows
    ]
