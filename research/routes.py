"""Research workspace routes: create, list, view and delete projects."""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func, or_, select

from auth.decorators import verified_required
from core.audit import record_event
from extensions import db, limiter
from reports.models import ReportDocument
from research.access import owned_project, projects_of
from research.constants import JOB_ACTIVE_STATUSES
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
    from research.services import can_resume, events_after, job_summary

    shown = next((j for j in jobs if j.is_active), None) or (jobs[0] if jobs else None)
    recent = events_after(shown, max(0, shown.event_seq - 60), limit=60) if shown else []
    return render_template(
        "research/detail.html", project=project, jobs=jobs, report_count=report_count, shown=shown, recent=recent,
        can_resume=can_resume, summary=job_summary(shown) if shown else None,
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


# ------------------------------------------------------------------------------------ running research
def _job_payload(job: ResearchJob, events) -> dict:
    from research.services import job_summary

    return {
        "summary": job_summary(job),
        "status": job.status,
        "stage": job.current_stage,
        "agent": job.current_agent,
        "finished": job.is_finished,
        "error": job.error_message,
        "last_seq": events[-1].seq if events else None,
        "events": [_event_dict(e) for e in events],
    }


def _event_dict(e) -> dict:
    return {
        "seq": e.seq, "type": e.event_type, "level": e.level, "stage": e.stage, "agent": e.agent,
        "message": e.message, "time": e.created_at.strftime("%H:%M:%S"),
    }  # fmt: skip


def _runner_problems(project: ResearchProject) -> list[str]:
    """Reasons a run cannot succeed right now, so the user hears it before waiting."""
    from providers import get_router
    from research_tools import get_search_client

    problems = []
    if not get_router().active_providers():
        problems.append(
            "No AI provider key is configured (set GEMINI_API_KEY_1, GROQ_API_KEY_1 or OPENROUTER_API_KEY_1)."
        )
    if not get_search_client().configured and not project.uploads:
        problems.append("Web search is not configured (set TAVILY_API_KEY) and no documents are attached.")
    return problems


@bp.post("/<public_id>/start")
@verified_required
@limiter.limit("10 per hour")
def start(public_id: str):
    from flask import current_app

    from jobs import enqueue
    from research.services import ActiveJobExistsError, create_job

    project = owned_project(current_user, public_id)
    if project.archived_at is not None:
        flash("Restore the project before starting a run.", "error")
        return redirect(url_for("research.detail", public_id=public_id))
    problems = _runner_problems(project)
    if problems:
        for text in problems:
            flash(text, "error")
        return redirect(url_for("research.detail", public_id=public_id))
    active = db.session.scalar(
        select(func.count()).select_from(ResearchJob).join(ResearchProject)
        .where(ResearchProject.user_id == current_user.id, ResearchJob.status.in_(JOB_ACTIVE_STATUSES))
    )  # fmt: skip
    if active >= current_app.config["MAX_ACTIVE_JOBS_PER_USER"]:
        flash("You already have the maximum number of runs in progress. Wait for one to finish.", "error")
        return redirect(url_for("research.detail", public_id=public_id))
    try:
        job = create_job(project)
    except ActiveJobExistsError:
        flash("This project already has a run in progress.", "error")
        return redirect(url_for("research.detail", public_id=public_id))
    enqueue(current_app._get_current_object(), job)  # noqa: SLF001
    flash("Research started. You can leave this page; it keeps running.", "success")
    return redirect(url_for("research.detail", public_id=public_id))


@bp.post("/<public_id>/jobs/<job_id>/cancel")
@verified_required
def cancel(public_id: str, job_id: str):
    from research.services import cancel_if_unstarted, request_cancel

    project = owned_project(current_user, public_id)
    job = _project_job(project, job_id)
    if request_cancel(job, current_user.id):
        cancel_if_unstarted(job)
        flash("Stopping the run. Progress made so far is kept.", "success")
    else:
        flash("That run has already finished.", "error")
    return redirect(url_for("research.detail", public_id=public_id))


@bp.post("/<public_id>/jobs/<job_id>/resume")
@verified_required
@limiter.limit("10 per hour")
def resume(public_id: str, job_id: str):
    from flask import current_app

    from jobs import enqueue
    from research.services import ActiveJobExistsError, resume_job

    project = owned_project(current_user, public_id)
    job = _project_job(project, job_id)
    problems = _runner_problems(project)
    if problems:
        for text in problems:
            flash(text, "error")
        return redirect(url_for("research.detail", public_id=public_id))
    try:
        ok = resume_job(job, current_user.id)
    except ActiveJobExistsError:
        flash("This project already has a run in progress.", "error")
        return redirect(url_for("research.detail", public_id=public_id))
    if not ok:
        flash("That run cannot be resumed.", "error")
        return redirect(url_for("research.detail", public_id=public_id))
    enqueue(current_app._get_current_object(), job)  # noqa: SLF001
    flash("Run resumed from where it stopped.", "success")
    return redirect(url_for("research.detail", public_id=public_id))


def _project_job(project: ResearchProject, job_id: str) -> ResearchJob:
    from flask import abort

    job = db.session.scalar(
        select(ResearchJob).where(ResearchJob.public_id == job_id, ResearchJob.project_id == project.id)
    )
    if job is None:
        abort(404)
    return job


@bp.get("/<public_id>/jobs/<job_id>/events")
@verified_required
@limiter.exempt
def job_events(public_id: str, job_id: str):
    """Progress as JSON (polling fallback and initial page data)."""
    from research.services import events_after

    job = _project_job(owned_project(current_user, public_id), job_id)
    after = max(0, request.args.get("after", 0, type=int) or 0)
    return jsonify(_job_payload(job, events_after(job, after)))


@bp.get("/<public_id>/jobs/<job_id>/stream")
@verified_required
@limiter.exempt
def job_stream(public_id: str, job_id: str):
    """Server-Sent Events. Each connection lasts at most JOB_SSE_MAX_SECONDS; the browser reconnects
    and resumes from ``Last-Event-ID``, so no progress is lost and no worker is held for long."""
    import json
    import time

    from flask import current_app

    from research.services import events_after

    job = _project_job(owned_project(current_user, public_id), job_id)
    job_pk = job.id
    last = request.headers.get("Last-Event-ID", type=int) or request.args.get("after", 0, type=int) or 0
    limit = current_app.config["JOB_SSE_MAX_SECONDS"]

    def generate():
        nonlocal last
        deadline = time.monotonic() + limit
        yield "retry: 3000\n\n"
        while True:
            db.session.rollback()  # end the previous read transaction so new rows are visible
            current = db.session.get(ResearchJob, job_pk)
            db.session.refresh(current)
            batch = events_after(current, last)
            for event in batch:
                last = event.seq
                yield f"id: {event.seq}\nevent: progress\ndata: {json.dumps(_event_dict(event))}\n\n"
            if batch:
                from research.services import job_summary

                yield f"event: summary\ndata: {json.dumps(job_summary(current))}\n\n"
            if current.is_finished and not batch:
                state = json.dumps({"status": current.status, "error": current.error_message})
                yield f"event: done\ndata: {state}\n\n"
                return
            if time.monotonic() >= deadline:
                yield ": reconnect\n\n"
                return
            yield ": keep-alive\n\n"
            time.sleep(1.0)

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    return response


# ------------------------------------------------------------------------------------------- report view
@bp.get("/<public_id>/report")
@verified_required
def report(public_id: str):
    from reports.render import number_sources, section_html
    from research.models import ResearchSource

    project = owned_project(current_user, public_id)
    doc = db.session.scalars(
        select(ReportDocument)
        .where(ReportDocument.project_id == project.id)
        .order_by(ReportDocument.id.desc())
    ).first()
    if doc is None:
        flash("There is no report yet.", "error")
        return redirect(url_for("research.detail", public_id=public_id))
    numbers = number_sources(doc)
    sources = {
        s.id: s
        for s in db.session.scalars(select(ResearchSource).where(ResearchSource.id.in_(numbers or {0})))
    }
    sections = [
        {"key": s.key, "heading": s.heading, "html": section_html(s.body, numbers)}
        for s in doc.sections if s.key != "references"
    ]  # fmt: skip
    references = [
        (n, sources[sid]) for sid, n in sorted(numbers.items(), key=lambda kv: kv[1]) if sid in sources
    ]
    job = db.session.get(ResearchJob, doc.job_id) if doc.job_id else None
    return render_template("research/report.html", project=project, report=doc, sections=sections,
                           references=references, job=job)  # fmt: skip


# ------------------------------------------------------------------------------- saved reports and history
def _paged(statement, page: int, size: int):
    total = db.session.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    pages = max(1, -(-total // size))
    page = min(page, pages)
    return total, pages, page


@bp.get("/reports")
@verified_required
def reports():
    """Every report the user has, newest first, with search."""
    query = (request.args.get("q") or "").strip()[:100]
    page = max(1, request.args.get("page", 1, type=int) or 1)
    statement = (
        select(ReportDocument, ResearchProject)
        .join(ResearchProject, ResearchProject.id == ReportDocument.project_id)
        .where(ResearchProject.user_id == current_user.id)
    )
    if query:
        like = f"%{_escape_like(query.lower())}%"
        statement = statement.where(
            or_(
                func.lower(ReportDocument.title).like(like, escape="\\"),
                func.lower(ResearchProject.title).like(like, escape="\\"),
            )
        )
    total, pages, page = _paged(statement, page, PAGE_SIZE)
    rows = db.session.execute(
        statement.order_by(ReportDocument.created_at.desc(), ReportDocument.id.desc())
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    ).all()
    return render_template(
        "research/reports.html", rows=rows, total=total, page=page, pages=pages, query=query
    )


@bp.get("/history")
@verified_required
def history():
    """Every run the user has started, newest first, with a status filter."""
    from research.constants import JOB_STATUSES
    from research.models import ResearchSource

    status = request.args.get("status", "")
    page = max(1, request.args.get("page", 1, type=int) or 1)
    statement = (
        select(ResearchJob, ResearchProject)
        .join(ResearchProject, ResearchProject.id == ResearchJob.project_id)
        .where(ResearchProject.user_id == current_user.id)
    )
    if status in JOB_STATUSES:
        statement = statement.where(ResearchJob.status == status)
    else:
        status = ""
    total, pages, page = _paged(statement, page, PAGE_SIZE)
    rows = db.session.execute(
        statement.order_by(ResearchJob.created_at.desc(), ResearchJob.id.desc())
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    ).all()
    ids = [job.id for job, _ in rows]
    source_counts = (
        dict(
            db.session.execute(
                select(ResearchSource.job_id, func.count())
                .where(ResearchSource.job_id.in_(ids))
                .group_by(ResearchSource.job_id)
            ).all()
        )
        if ids
        else {}
    )
    return render_template("research/history.html", rows=rows, total=total, page=page, pages=pages, status=status,
                           statuses=JOB_STATUSES, source_counts=source_counts)  # fmt: skip
