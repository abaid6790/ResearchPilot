"""Research workspace routes: create, list, view and delete projects."""

from __future__ import annotations

import logging

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func, or_, select

from auth.decorators import verified_required
from core.audit import record_event
from extensions import db, limiter
from reports.models import ReportDocument
from research.access import owned_project, projects_of
from research.forms import CATEGORY_LABELS, DEPTH_LABELS, NewResearchForm, limits_for_template
from research.models import ResearchJob, ResearchProject
from research.schemas import COSTLY_SETTINGS
from research.services import create_project

logger = logging.getLogger(__name__)
bp = Blueprint("research", __name__, url_prefix="/research")

PAGE_SIZE = 12


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@bp.get("/")
@verified_required
def index():
    """All of the user's projects, newest first, with search and an archive filter."""
    query = (request.args.get("q") or "").strip()[:100]
    show = request.args.get("show", "active")
    page = max(1, request.args.get("page", 1, type=int) or 1)

    statement = projects_of(current_user)
    if show == "archived":
        statement = statement.where(ResearchProject.archived_at.is_not(None))
    else:
        show = "active"
        statement = statement.where(ResearchProject.archived_at.is_(None))
    if query:
        like = f"%{_escape_like(query.lower())}%"
        statement = statement.where(
            or_(
                func.lower(ResearchProject.title).like(like, escape="\\"),
                func.lower(ResearchProject.topic).like(like, escape="\\"),
            )
        )
    total = db.session.scalar(select(func.count()).select_from(statement.subquery()))
    projects = db.session.scalars(
        statement.order_by(ResearchProject.created_at.desc(), ResearchProject.id.desc())
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    ).all()
    return render_template(
        "research/list.html",
        projects=projects, total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)),
        query=query, show=show, latest_status=_latest_job_status(projects), depth_labels=DEPTH_LABELS,
    )  # fmt: skip


def _latest_job_status(projects) -> dict[int, str]:
    """Latest job status per project (one query for the whole page)."""
    ids = [p.id for p in projects]
    if not ids:
        return {}
    latest = (
        select(ResearchJob.project_id, func.max(ResearchJob.id).label("job_id"))
        .where(ResearchJob.project_id.in_(ids))
        .group_by(ResearchJob.project_id)
        .subquery()
    )
    rows = db.session.execute(
        select(ResearchJob.project_id, ResearchJob.status).join(latest, ResearchJob.id == latest.c.job_id)
    ).all()
    return dict(rows)


@bp.route("/new", methods=["GET", "POST"])
@verified_required
@limiter.limit("30 per hour", methods=["POST"])
def new():
    form = NewResearchForm()
    if form.validate_on_submit():
        settings = form.build_settings()
        project = create_project(
            current_user, title=form.title.data, topic=form.topic.data, objectives=form.objectives.data,
            settings=settings.model_dump(mode="json") if settings else {},
        )  # fmt: skip
        from research.services import attach_documents

        attach_documents(project, getattr(form, "parsed_documents", []))
        flash("Research project saved.", "success")
        return redirect(url_for("research.detail", public_id=project.public_id))
    return render_template(
        "research/new.html", form=form, limits=limits_for_template(), costly=COSTLY_SETTINGS,
    ), (400 if form.is_submitted() else 200)  # fmt: skip


@bp.get("/<public_id>")
@verified_required
def detail(public_id: str):
    project = owned_project(current_user, public_id)
    jobs = list(reversed(project.jobs))
    report_count = db.session.scalar(
        select(func.count()).select_from(ReportDocument).where(ReportDocument.project_id == project.id)
    )
    return render_template(
        "research/detail.html", project=project, jobs=jobs, report_count=report_count,
        settings=project.settings, depth_labels=DEPTH_LABELS, category_labels=CATEGORY_LABELS,
        costly=COSTLY_SETTINGS, active_job=next((j for j in jobs if j.is_active), None),
    )  # fmt: skip


@bp.post("/<public_id>/archive")
@verified_required
def archive(public_id: str):
    from core.db_utils import utcnow

    project = owned_project(current_user, public_id)
    restoring = project.archived_at is not None
    project.archived_at = None if restoring else utcnow()
    db.session.commit()
    flash("Project restored." if restoring else "Project archived.", "success")
    if restoring:
        return redirect(url_for("research.detail", public_id=public_id))
    return redirect(url_for("research.index"))


@bp.post("/<public_id>/delete")
@verified_required
@limiter.limit("30 per hour")
def delete(public_id: str):
    project = owned_project(current_user, public_id)
    if any(job.is_active for job in project.jobs):
        flash("Cancel the running research job before deleting this project.", "warning")
        return redirect(url_for("research.detail", public_id=public_id))
    if (request.form.get("confirm_title") or "").strip() != project.title:
        flash("The title you typed did not match, so nothing was deleted.", "error")
        return redirect(url_for("research.detail", public_id=public_id))
    record_event(
        "project_deleted", actor_user_id=current_user.id, target_type="project", target_id=project.public_id
    )
    db.session.delete(project)
    db.session.commit()
    flash("Project and everything saved under it were deleted.", "success")
    return redirect(url_for("research.index"))
