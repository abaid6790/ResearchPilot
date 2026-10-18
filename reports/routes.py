"""Source explorer and report editor routes (all under /research/<project>/...)."""

from __future__ import annotations

from flask import Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import exists, func, or_, select

from auth.decorators import verified_required
from core.audit import record_event
from extensions import db, limiter
from reports import editing
from reports.editing import EditError
from reports.models import ReportDocument, ReportRevision
from reports.render import number_sources, to_plain_text
from research import constants as c
from research.access import owned_project
from research.models import (
    ClaimSource,
    EvidenceExcerpt,
    ResearchClaim,
    ResearchJob,
    ResearchProject,
    ResearchSource,
    ResearchTask,
)

bp = Blueprint("editor", __name__, url_prefix="/research")

PAGE_SIZE = 15
PROBLEM_FETCH = ("failed", "blocked", "paywalled", "snippet_only")
SUPPORT_LABELS = {
    "direct": "Excerpt directly supports a claim",
    "partial": "Excerpt partly supports a claim",
    "unrelated": "Excerpt did not support its claim",
    "unreviewed": "Not yet reviewed",
    "none": "No claim uses this source",
}


def _latest_report(project: ResearchProject) -> ReportDocument | None:
    return db.session.scalars(
        select(ReportDocument)
        .where(ReportDocument.project_id == project.id)
        .order_by(ReportDocument.id.desc())
    ).first()


def _latest_job(project: ResearchProject) -> ResearchJob | None:
    return db.session.scalars(
        select(ResearchJob).where(ResearchJob.project_id == project.id).order_by(ResearchJob.id.desc())
    ).first()


def _like(text: str) -> str:
    return "%" + text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def safe_url(url: str | None) -> str | None:
    """Only http(s) links are ever rendered as links (never javascript: or data: URLs)."""
    return url if url and url.lower().startswith(("http://", "https://")) else None


def _support_label(levels: list[str]) -> tuple[str, str]:
    if not levels:
        return "none", SUPPORT_LABELS["none"]
    for key in ("direct", "partial", "unrelated"):
        if key in levels:
            return key, SUPPORT_LABELS[key]
    return "unreviewed", SUPPORT_LABELS["unreviewed"]


def source_warnings(src: ResearchSource) -> list[str]:
    out = []
    if src.fetch_status == "snippet_only":
        out.append("Only a search snippet was available; the page was not read in full.")
    elif src.fetch_status in ("failed", "blocked", "paywalled"):
        out.append({"failed": "The page could not be retrieved.", "blocked": "The page could not be fetched safely.",
                    "paywalled": "The page is behind a paywall."}[src.fetch_status])  # fmt: skip
    if src.link_status in ("unreachable", "unsafe"):
        out.append(f"The link is {src.link_status}.")
    if src.metadata_status == "mismatch":
        out.append("Title or date from the search did not match the page.")
    out += [str(w)[:200] for w in (src.warnings or [])]
    return out


# ============================================================================ source explorer
@bp.get("/<public_id>/sources")
@verified_required
def sources(public_id: str):
    project = owned_project(current_user, public_id)
    job = _latest_job(project)
    report = _latest_report(project)
    q = (request.args.get("q") or "").strip()[:100]
    domain = (request.args.get("domain") or "").strip()[:255]
    category = request.args.get("category") or ""
    status = request.args.get("status") or ""
    page = max(1, request.args.get("page", 1, type=int) or 1)
    base = select(ResearchSource).where(ResearchSource.job_id == (job.id if job else 0))
    domains = (
        list(db.session.scalars(
            select(ResearchSource.domain).where(ResearchSource.job_id == (job.id if job else 0),
                                                 ResearchSource.domain.is_not(None)).distinct().order_by(ResearchSource.domain)
        ))
        if job else []
    )  # fmt: skip
    numbers = number_sources(report) if report else {}
    stmt = base
    if q:
        like = _like(q)
        stmt = stmt.where(or_(
            func.lower(func.coalesce(ResearchSource.title, "")).like(like, escape="\\"),
            func.lower(func.coalesce(ResearchSource.domain, "")).like(like, escape="\\"),
            exists().where(EvidenceExcerpt.source_id == ResearchSource.id,
                           func.lower(EvidenceExcerpt.text).like(like, escape="\\")),
        ))  # fmt: skip
    if domain:
        stmt = stmt.where(ResearchSource.domain == domain)
    if category in c.SOURCE_CATEGORIES:
        stmt = stmt.where(ResearchSource.category == category)
    else:
        category = ""
    if status == "cited":
        stmt = stmt.where(ResearchSource.id.in_(list(numbers) or [0]))
    elif status == "uncited":
        stmt = stmt.where(ResearchSource.id.not_in(list(numbers) or [0]))
    elif status == "problems":
        stmt = stmt.where(or_(ResearchSource.fetch_status.in_(PROBLEM_FETCH),
                              ResearchSource.link_status.in_(("unreachable", "unsafe")),
                              ResearchSource.metadata_status == "mismatch"))  # fmt: skip
    else:
        status = ""
    total = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    rows = list(db.session.scalars(
        stmt.order_by(ResearchSource.rank, ResearchSource.id).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
    ))  # fmt: skip
    ids = [s.id for s in rows] or [0]
    levels: dict[int, list[str]] = {}
    claim_counts: dict[int, int] = {}
    for sid, level, n in db.session.execute(
        select(ClaimSource.source_id, ClaimSource.support_level, func.count())
        .where(ClaimSource.source_id.in_(ids)).group_by(ClaimSource.source_id, ClaimSource.support_level)
    ):  # fmt: skip
        levels.setdefault(sid, []).append(level)
        claim_counts[sid] = claim_counts.get(sid, 0) + n
    tasks = {t.id: t.title for t in db.session.scalars(
        select(ResearchTask).where(ResearchTask.id.in_([s.task_id for s in rows if s.task_id] or [0])))}  # fmt: skip
    excerpts = {}
    for ex in db.session.scalars(
        select(EvidenceExcerpt).where(EvidenceExcerpt.source_id.in_(ids)).order_by(EvidenceExcerpt.id)
    ):
        excerpts.setdefault(ex.source_id, ex)
    items = []
    for s in rows:
        key, label = _support_label(levels.get(s.id, []))
        ex = excerpts.get(s.id)
        items.append({
            "source": s, "url": safe_url(s.url), "number": numbers.get(s.id), "question": tasks.get(s.task_id),
            "excerpt": (ex.text[:300] + ("..." if len(ex.text) > 300 else "")) if ex else None,
            "claims": claim_counts.get(s.id, 0), "support": key, "support_label": label,
            "warnings": source_warnings(s),
        })  # fmt: skip
    return render_template(
        "reports/sources.html", project=project, job=job, items=items, total=total, page=page, pages=pages,
        q=q, domain=domain, category=category, status=status, domains=domains,
        categories=c.SOURCE_CATEGORIES, report=report,
    )  # fmt: skip


@bp.get("/<public_id>/sources/<int:source_id>")
@verified_required
def source_detail(public_id: str, source_id: int):
    project = owned_project(current_user, public_id)
    src = db.session.scalars(
        select(ResearchSource)
        .join(ResearchJob)
        .where(ResearchSource.id == source_id, ResearchJob.project_id == project.id)
    ).first()
    if src is None:
        abort(404)
    report = _latest_report(project)
    numbers = number_sources(report) if report else {}
    claims = []
    for claim, link in db.session.execute(
        select(ResearchClaim, ClaimSource)
        .join(ClaimSource, (ClaimSource.claim_id == ResearchClaim.id) & (ClaimSource.job_id == ResearchClaim.job_id))
        .where(ClaimSource.source_id == src.id).order_by(ResearchClaim.id)
    ):  # fmt: skip
        claims.append({"claim": claim, "link": link})
    used_in = []
    if report and src.job_id == report.job_id:
        for section in report.sections:
            if f"[@S{src.id}]" in section.body and section.key != "references":
                used_in.append(section)
    task = db.session.get(ResearchTask, src.task_id) if src.task_id else None
    levels = [x["link"].support_level for x in claims]
    key, label = _support_label(levels)
    return render_template(
        "reports/source_detail.html", project=project, src=src, url=safe_url(src.url), excerpts=src.excerpts,
        claims=claims, used_in=used_in, number=numbers.get(src.id), question=task.title if task else None,
        warnings=source_warnings(src), support=key, support_label=label, report=report,
    )  # fmt: skip


# ============================================================================ report editor
def _editor_context(public_id: str):
    project = owned_project(current_user, public_id)
    report = _latest_report(project)
    if report is None:
        abort(404)
    return project, report


def _fail(exc: EditError, project, wants_json: bool, section_key: str | None = None):
    if wants_json:
        return jsonify(error=exc.code, message=exc.message), exc.status
    flash(exc.message, "error")
    if exc.status == 404 and section_key is None:
        abort(404)
    return redirect(url_for("editor.edit", public_id=project.public_id, section=section_key))


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


@bp.get("/<public_id>/report/edit")
@verified_required
def edit(public_id: str):
    project, report = _editor_context(public_id)
    sections = [s for s in report.sections if s.key != "references"]
    if not sections:
        flash("This report has no sections to edit.", "error")
        return redirect(url_for("research.report", public_id=public_id))
    key = request.args.get("section") or sections[0].key
    section = next((s for s in sections if s.key == key), None)
    if section is None:
        abort(404)
    revisions = list(db.session.scalars(
        select(ReportRevision).where(ReportRevision.report_id == report.id, ReportRevision.section_key == section.key,
                                     ReportRevision.status == "applied").order_by(ReportRevision.revision_no.desc()).limit(30)
    ))  # fmt: skip
    proposals = editing.pending_proposals(report, section)
    compare = None
    cid = request.args.get("compare", type=int)
    if cid:
        rev = db.session.get(ReportRevision, cid)
        if rev is None or rev.report_id != report.id or rev.section_key != section.key:
            abort(404)
        compare = {"revision": rev, "lines": editing.diff_lines(rev.content or "", section.body)}
    for p in proposals:
        p.diff = editing.diff_lines(section.body, p.content or "")  # type: ignore[attr-defined]
    numbers = number_sources(report)
    cited = editing.section_citation_ids(section.body)
    src = {
        s.id: s for s in db.session.scalars(select(ResearchSource).where(ResearchSource.id.in_(cited or [0])))
    }
    checks = editing.check_body(report, section.body)
    return render_template(
        "reports/editor.html", project=project, report=report, sections=sections, section=section,
        revisions=revisions, proposals=proposals, compare=compare, checks=checks, cited=[
            {"id": i, "number": numbers.get(i), "source": src.get(i)} for i in cited
        ], busy=editing.run_is_active(report), max_body=editing.MAX_BODY,
    )  # fmt: skip


@bp.post("/<public_id>/report/edit/<key>/save")
@verified_required
@limiter.limit("240 per hour")
def save(public_id: str, key: str):
    project, report = _editor_context(public_id)
    wants_json = _wants_json()
    try:
        section = editing.find_section(report, key)
        section, checks = editing.save_section(
            report,
            section,
            request.form.get("body", ""),
            request.form.get("version", type=int) or -1,
            current_user,
            autosave=request.form.get("autosave") == "1",
        )
        autosave = request.form.get("autosave") == "1"
        if not autosave:
            record_event("report_edited", actor_user_id=current_user.id, target_type="report",
                         target_id=report.public_id, section=key)  # fmt: skip
            db.session.commit()
    except EditError as exc:
        return _fail(exc, project, wants_json, key)
    if wants_json:
        return jsonify(ok=True, version=section.version, checks=checks.as_dict())
    flash("Section saved.", "success")
    return redirect(url_for("editor.edit", public_id=public_id, section=key))


@bp.post("/<public_id>/report/edit/<key>/regenerate")
@verified_required
@limiter.limit("20 per hour")
def regenerate(public_id: str, key: str):
    project, report = _editor_context(public_id)
    try:
        section = editing.find_section(report, key)
        editing.propose_regeneration(report, section, request.form.get("instruction", ""), current_user)
    except EditError as exc:
        return _fail(exc, project, False, key)
    flash("A suggested rewrite is ready. Compare it with your text, then apply or discard it.", "success")
    return redirect(url_for("editor.edit", public_id=public_id, section=key) + "#proposals")


@bp.post("/<public_id>/report/edit/<key>/critique")
@verified_required
@limiter.limit("20 per hour")
def critique(public_id: str, key: str):
    project, report = _editor_context(public_id)
    try:
        section = editing.find_section(report, key)
        result = editing.critique_section(report, section, current_user)
    except EditError as exc:
        return _fail(exc, project, True, key)
    return jsonify(ok=True, assessment=result.assessment, notes=[n.model_dump() for n in result.notes])


@bp.post("/<public_id>/report/revisions/<int:revision_id>/<action>")
@verified_required
@limiter.limit("120 per hour")
def revision_action(public_id: str, revision_id: int, action: str):
    project, report = _editor_context(public_id)
    if action not in ("apply", "discard", "restore"):
        abort(404)
    rev = db.session.get(ReportRevision, revision_id)
    if rev is None or rev.report_id != report.id or not rev.section_key:
        abort(404)
    try:
        section = editing.find_section(report, rev.section_key)
        if action == "apply":
            editing.apply_proposal(report, section, rev, current_user)
            record_event("report_edited", actor_user_id=current_user.id, target_type="report",
                         target_id=report.public_id, section=section.key, via="suggestion")  # fmt: skip
            flash("Suggestion applied. The previous text stays in the history.", "success")
        elif action == "discard":
            editing.discard_proposal(report, section, rev)
            flash("Suggestion discarded.", "success")
        else:
            editing.restore_revision(
                report, section, rev, request.form.get("version", type=int) or -1, current_user
            )
            record_event("report_restored", actor_user_id=current_user.id, target_type="report",
                         target_id=report.public_id, section=section.key, revision=rev.revision_no)  # fmt: skip
            flash(f"Restored version {rev.revision_no}.", "success")
        db.session.commit()
    except EditError as exc:
        return _fail(exc, project, False, rev.section_key)
    return redirect(url_for("editor.edit", public_id=public_id, section=rev.section_key))


@bp.get("/<public_id>/report/text")
@verified_required
def text(public_id: str):
    _, report = _editor_context(public_id)
    return Response(to_plain_text(report, {}), mimetype="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})  # fmt: skip
