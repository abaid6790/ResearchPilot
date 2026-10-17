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
    return render_template("dashboard/index.html", stats=stats, recent=recent)
