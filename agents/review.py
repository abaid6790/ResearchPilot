"""Critic (independent review) and the revision loop."""

from __future__ import annotations

from sqlalchemy import select

from agents import prompts
from agents.citations import clean_citations
from agents.context import AgentContext
from agents.schemas import CriticReview, SectionDraft
from agents.writer import NOT_COVERED, add_revision, claims_for_section, format_claims
from extensions import db
from reports.models import ReportDocument
from research.models import ResearchClaim
from research_tools.untrusted import wrap_untrusted


def current_report(job_id: int) -> ReportDocument:
    return db.session.scalars(select(ReportDocument).where(ReportDocument.job_id == job_id)).one()


def run_critic(ctx: AgentContext) -> None:
    job = ctx.job
    ctx.set_stage("reviewing", "critic", "An independent critic is reviewing the draft.")
    report = current_report(job.id)
    claims = list(db.session.scalars(select(ResearchClaim).where(ResearchClaim.job_id == job.id).limit(80)))
    claim_lines = "\n".join(
        f"C{c.id}: {c.text} (sources: {', '.join(f'S{i}' for i in dict.fromkeys(link.source_id for link in c.links))})"
        for c in claims
    )
    draft = "\n\n".join(f"## [{s.key}] {s.heading}\n{s.body}" for s in report.sections)
    outline = (job.checkpoint or {}).get("outline") or []
    prompt = (
        f"Research request: {ctx.project.topic}\nPlanned sections: {[o['key'] for o in outline]}\n\n"
        + wrap_untrusted(f"CLAIMS AVAILABLE:\n{claim_lines}", label="claims") + "\n\n"
        + wrap_untrusted(f"DRAFT:\n{draft}", label="draft")
    )  # fmt: skip
    review = ctx.llm("critic", prompts.CRITIC, prompt, CriticReview, max_output_tokens=3000)
    keys = {s.key for s in report.sections}
    issues = [i.model_dump() for i in review.issues if i.section_key in keys][:14]
    job.checkpoint = {
        **(job.checkpoint or {}),
        "review": {"issues": issues, "assessment": review.assessment[:800]},
    }
    db.session.commit()
    major = sum(1 for i in issues if i["severity"] == "major")
    ctx.event("critic_review", f"Critic found {len(issues)} issue(s), {major} major.", stage="reviewing", agent="critic",
              level="warning" if major else "info", data={"issues": len(issues), "major": major})  # fmt: skip


def major_issues(job) -> list[dict]:
    return [
        i for i in ((job.checkpoint or {}).get("review") or {}).get("issues", []) if i["severity"] == "major"
    ]


def run_revision(ctx: AgentContext) -> None:
    job = ctx.job
    job.revision_round += 1
    ctx.set_stage("writing", "writer", f"Revising the draft after review (round {job.revision_round}).")
    report = current_report(job.id)
    outline = {o["key"]: o for o in (job.checkpoint or {}).get("outline") or []}
    by_section: dict[str, list[dict]] = {}
    for issue in major_issues(job):
        by_section.setdefault(issue["section_key"], []).append(issue)
    for section in report.sections:
        issues = by_section.get(section.key)
        if not issues or section.edited_by_user:
            continue  # never overwrite a user's own edits
        ctx.check_cancel()
        claims = claims_for_section(job.id, (outline.get(section.key) or {}).get("subtopics") or [])
        if not claims:
            continue
        block, allowed = format_claims(claims)
        issue_text = "\n".join(
            f"- ({i['kind']}) {i['problem']} Suggestion: {i['suggestion']}" for i in issues
        )
        prompt = (
            f"Section: {section.heading}\nCurrent text:\n{section.body}\n\nIssues to fix:\n{issue_text}\n\n"
            + wrap_untrusted(block, label="claims")
        )
        draft = ctx.llm("writer", prompts.REVISER, prompt, SectionDraft, max_output_tokens=3000)
        body, _ = clean_citations(draft.body.strip(), allowed)
        if body and body != NOT_COVERED:
            section.body, section.version = body, section.version + 1
            add_revision(report, kind="section", source="agent_editor", section=section,
                         summary=f"Revised after review: {len(issues)} issue(s)")  # fmt: skip
            ctx.event(
                "section_written", f"Revised section: {section.heading[:80]}", stage="writing", agent="writer"
            )
    db.session.commit()
