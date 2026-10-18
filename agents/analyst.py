"""Analyst: extracts claims, each tied to stored excerpts, and judges whether evidence is sufficient."""

from __future__ import annotations

from sqlalchemy import delete, or_, select

from agents import prompts
from agents.context import AgentContext
from agents.schemas import SubtopicFindings
from extensions import db
from research.models import ClaimSource, EvidenceExcerpt, ResearchClaim, ResearchSource, ResearchTask
from research_tools.untrusted import wrap_untrusted

MAX_EXCERPTS = 24
EXCERPT_CHARS = 700
SNIPPET_ONLY_CAP = 0.6  # confidence ceiling when every cited excerpt is only a search snippet


def _excerpts_for(job_id: int, task_id: int) -> list[tuple[EvidenceExcerpt, ResearchSource]]:
    rows = db.session.execute(
        select(EvidenceExcerpt, ResearchSource)
        .join(ResearchSource, ResearchSource.id == EvidenceExcerpt.source_id)
        .where(
            ResearchSource.job_id == job_id,
            or_(ResearchSource.task_id == task_id, ResearchSource.origin == "upload"),
        )
        .order_by(EvidenceExcerpt.relevance_score.desc().nullslast(), EvidenceExcerpt.id)
        .limit(MAX_EXCERPTS)
    ).all()
    return [(e, s) for e, s in rows]


def run_analysis(ctx: AgentContext) -> None:
    job = ctx.job
    ctx.set_stage("analyzing", "analyst", "Extracting claims from the evidence.")
    gaps: dict[str, str] = {}
    tasks = list(db.session.scalars(
        select(ResearchTask).where(ResearchTask.job_id == job.id, ResearchTask.status == "done",
                                   ResearchTask.result_summary.is_(None)).order_by(ResearchTask.position)
    ))  # fmt: skip
    for task in tasks:
        ctx.check_cancel()
        rows = _excerpts_for(job.id, task.id)
        if not rows:
            task.result_summary = "No readable evidence was found for this subtopic."
            gaps[str(task.id)] = "no evidence was found"
            db.session.commit()
            ctx.event("claims_extracted", f"No evidence for: {task.title[:100]}", level="warning",
                      stage="analyzing", agent="analyst")  # fmt: skip
            continue
        # replace claims from an earlier round: the new evidence set is a superset
        old = db.session.scalars(select(ResearchClaim.id).where(ResearchClaim.task_id == task.id)).all()
        if old:
            db.session.execute(delete(ClaimSource).where(ClaimSource.claim_id.in_(old)))
            db.session.execute(delete(ResearchClaim).where(ResearchClaim.id.in_(old)))
        by_id = {f"E{e.id}": (e, s) for e, s in rows}
        block = "\n\n".join(
            f"[E{e.id}] (source S{s.id}, {s.domain or s.upload_name}, "
            f"{'full page' if s.fetch_status == 'retrieved' else 'search snippet only'})\n{e.text[:EXCERPT_CHARS]}"
            for e, s in rows
        )
        prompt = f"Subtopic: {task.title}\nQuestions:\n{task.description}\n\n" + wrap_untrusted(
            block, label=f"excerpts for {task.title[:60]}"
        )
        findings = ctx.llm("analyst", prompts.ANALYST, prompt, SubtopicFindings, max_output_tokens=4000)

        kept = dropped = 0
        for position, fc in enumerate(findings.claims):
            links, seen = [], set()
            for ev in fc.evidence:
                found = by_id.get(ev.excerpt_id.strip())
                if found is None or (found[1].id, ev.relation) in seen:
                    continue  # the model cited an excerpt it was never given: ignore the citation
                seen.add((found[1].id, ev.relation))
                links.append((found, ev.relation))
            if not links:
                dropped += 1
                continue
            confidence = fc.confidence
            if all(s.fetch_status != "retrieved" for (_, s), _ in links):
                confidence = min(confidence, SNIPPET_ONLY_CAP)
            claim = ResearchClaim(job_id=job.id, task_id=task.id, text=fc.text.strip(), claim_type=fc.claim_type,
                                  confidence=round(confidence, 2), position=position)  # fmt: skip
            db.session.add(claim)
            db.session.flush()
            for (excerpt, source), relation in links:
                db.session.add(ClaimSource(claim_id=claim.id, source_id=source.id, job_id=job.id,
                                           excerpt_id=excerpt.id, relation=relation))  # fmt: skip
            kept += 1
        task.result_summary = (findings.summary or "Analysed.")[:2000]
        if not findings.sufficient or kept == 0:
            gaps[str(task.id)] = findings.missing or "no supported claims could be extracted"
        db.session.commit()
        ctx.event("claims_extracted", f"{kept} claim(s) for: {task.title[:100]}" + (f" ({dropped} dropped: no valid evidence)" if dropped else ""),
                  stage="analyzing", agent="analyst", data={"claims": kept, "dropped": dropped})  # fmt: skip
    job.checkpoint = {**(job.checkpoint or {}), "gaps": gaps}
    db.session.commit()
