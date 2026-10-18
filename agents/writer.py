"""Writer: drafts each report section from the extracted claims only."""

from __future__ import annotations

from sqlalchemy import select

from agents import prompts
from agents.citations import clean_citations
from agents.context import AgentContext
from agents.schemas import SectionDraft
from extensions import db
from reports.models import ReportDocument, ReportRevision, ReportSection
from research.models import ResearchClaim, ResearchSource, ResearchTask
from research_tools.untrusted import wrap_untrusted

WORDS = {"short": 120, "medium": 250, "long": 450, "very_long": 700}
MAX_CLAIMS_PER_SECTION = 25
NOT_COVERED = "The evidence collected for this run does not cover this topic."


def claims_for_section(job_id: int, subtopics: list[str]) -> list[ResearchClaim]:
    stmt = select(ResearchClaim).where(ResearchClaim.job_id == job_id)
    if subtopics:
        task_ids = db.session.scalars(
            select(ResearchTask.id).where(ResearchTask.job_id == job_id, ResearchTask.title.in_(subtopics))
        ).all()
        stmt = stmt.where(ResearchClaim.task_id.in_(task_ids))
    claims = list(
        db.session.scalars(stmt.order_by(ResearchClaim.confidence.desc().nullslast(), ResearchClaim.id))
    )
    return claims[:MAX_CLAIMS_PER_SECTION]


def format_claims(claims: list[ResearchClaim]) -> tuple[str, set[int]]:
    allowed: set[int] = set()
    lines, labels = [], {}
    for claim in claims:
        ids = []
        for link in claim.links:
            if link.relation == "context" and len(claim.links) > 1:
                continue
            ids.append(link.source_id)
            allowed.add(link.source_id)
        tag = ", ".join(f"S{i}" for i in dict.fromkeys(ids))
        rel = (
            "; contradicted by some source"
            if any(link.relation == "contradicts" for link in claim.links)
            else ""
        )
        lines.append(
            f"C{claim.id} [{claim.claim_type}, confidence {claim.confidence}{rel}] {claim.text} (sources: {tag})"
        )
    for src in db.session.scalars(select(ResearchSource).where(ResearchSource.id.in_(allowed or {0}))):
        when = src.published_at.strftime("%Y-%m-%d") if src.published_at else "undated"
        labels[src.id] = f"S{src.id} = {src.title or src.domain} ({src.domain or src.upload_name}, {when})"
    return "CLAIMS:\n" + "\n".join(lines) + "\n\nSOURCES:\n" + "\n".join(
        labels[k] for k in sorted(labels)
    ), allowed


def add_revision(report: ReportDocument, *, kind: str, source: str, section: ReportSection | None = None,
                 structure: dict | None = None, summary: str | None = None) -> ReportRevision:  # fmt: skip
    report.current_revision_no += 1
    rev = ReportRevision(
        report_id=report.id, revision_no=report.current_revision_no, kind=kind, section_id=section.id if section else None,
        section_key=section.key if section else None, source=source, status="applied",
        heading=section.heading if section else None, content=section.body if section else None, structure=structure,
        change_summary=summary,
    )  # fmt: skip
    db.session.add(rev)
    return rev


def snapshot(report: ReportDocument) -> dict:
    return {"sections": [{"key": s.key, "heading": s.heading, "body": s.body} for s in report.sections]}


def run_writer(ctx: AgentContext) -> None:
    job, project, st = ctx.job, ctx.project, ctx.settings
    ctx.set_stage("writing", "writer", "Writing the report.")
    outline = (job.checkpoint or {}).get("outline") or []
    report = ReportDocument(project_id=project.id, job_id=job.id, title=project.title[:300], language=st.language,
                            citation_style=st.citation_style)  # fmt: skip
    db.session.add(report)
    db.session.flush()
    for position, sec in enumerate(outline):
        ctx.check_cancel()
        claims = claims_for_section(job.id, sec.get("subtopics") or [])
        removed: list[int] = []
        if not claims:
            body = NOT_COVERED
        else:
            block, allowed = format_claims(claims)
            prompt = (
                f"Section: {sec['heading']}\nPurpose: {sec.get('purpose') or '(not given)'}\n"
                f"Audience: {st.audience}. Style: {st.writing_style}. Language: {st.language}. "
                f"Aim for about {WORDS[st.output_length]} words.\n\n" + wrap_untrusted(block, label="claims")
            )
            draft = ctx.llm("writer", prompts.WRITER, prompt, SectionDraft, max_output_tokens=3000)
            body, removed = clean_citations(draft.body.strip(), allowed)
        db.session.add(ReportSection(report_id=report.id, key=sec["key"], heading=sec["heading"], level=1,
                                     position=position, body=body))  # fmt: skip
        db.session.flush()
        if removed:
            ctx.event("warning", f"Removed {len(removed)} citation(s) to sources not given for '{sec['heading'][:60]}'.",
                      level="warning", stage="writing", agent="writer", data={"removed": removed})  # fmt: skip
        ctx.event("section_written", f"Wrote section: {sec['heading'][:80]}", stage="writing", agent="writer")
    db.session.refresh(report)
    add_revision(
        report, kind="document", source="agent_writer", structure=snapshot(report), summary="First draft"
    )
    db.session.commit()
