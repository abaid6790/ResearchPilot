"""Validator: deterministic citation checks, per-claim support checks, and finalisation.

Reachable URL != supported claim. Support is judged per (claim, excerpt) pair and stored on the
link; the report is flagged when claims lack direct support.
"""

from __future__ import annotations

from sqlalchemy import select

from agents import prompts
from agents.citations import cited_ids, clean_citations, uncited_paragraphs, unverified_quotes
from agents.context import AgentContext, AgentFailure
from agents.review import current_report, major_issues
from agents.schemas import ValidationBatch
from agents.writer import NOT_COVERED, add_revision, snapshot
from core.db_utils import utcnow
from extensions import db
from research import constants as c
from research.models import ClaimSource, EvidenceExcerpt, ResearchClaim, ResearchSource
from research_tools.untrusted import wrap_untrusted

MAX_PAIRS = 60
BATCH = 8
EXCERPT_CHARS = 600


def _validate_pairs(ctx: AgentContext) -> bool:
    """LLM support check per claim/excerpt pair. Returns False when it could not complete."""
    job = ctx.job
    links = list(db.session.execute(
        select(ClaimSource, ResearchClaim, EvidenceExcerpt)
        .join(ResearchClaim, ResearchClaim.id == ClaimSource.claim_id)
        .join(EvidenceExcerpt, EvidenceExcerpt.id == ClaimSource.excerpt_id)
        .where(ClaimSource.job_id == job.id, ClaimSource.relation != "context", ClaimSource.support_level == "unreviewed")
        .order_by(ResearchClaim.confidence.desc().nullslast(), ClaimSource.id).limit(MAX_PAIRS)
    ))  # fmt: skip
    complete = True
    for start in range(0, len(links), BATCH):
        batch = links[start : start + BATCH]
        block = "\n\n".join(
            f"[P{link.id}] CLAIM ({link.relation}): {claim.text}\nEXCERPT: {ex.text[:EXCERPT_CHARS]}"
            for link, claim, ex in batch
        )
        try:
            result = ctx.llm(
                "validator",
                prompts.VALIDATOR,
                wrap_untrusted(block, label="pairs"),
                ValidationBatch,
                max_output_tokens=2000,
            )
        except AgentFailure as exc:
            if exc.code in ("budget_exceeded", "llm_unavailable", "llm_bad_output", "llm_rejected"):
                complete = False
                ctx.event("warning", "Support checking could not finish; remaining claims are marked not assessed.",
                          level="warning", stage="validating", agent="validator", data={"code": exc.code})  # fmt: skip
                break
            raise
        verdicts = {v.pair_id.strip(): v for v in result.verdicts}
        for link, _claim, _ex in batch:
            v = verdicts.get(f"P{link.id}")
            if v is not None:
                link.support_level = v.support
                link.note = v.reason[:500] or None
        db.session.commit()
    return complete


def _claim_status(claim: ResearchClaim) -> str:
    supports = [lk for lk in claim.links if lk.relation == "supports"]
    if any(lk.support_level == "direct" for lk in supports):
        return "supported"
    if any(lk.support_level == "partial" for lk in supports):
        return "partial"
    if any(lk.relation == "contradicts" and lk.support_level in ("direct", "partial") for lk in claim.links):
        return "contradicted"
    if supports and all(lk.support_level == "unrelated" for lk in supports):
        return "insufficient"
    return "unassessed"


def run_validation(ctx: AgentContext) -> list[dict]:
    job = ctx.job
    ctx.set_stage("validating", "validator", "Checking citations and whether sources support the claims.")
    report = current_report(job.id)
    issues: list[dict] = []
    job_source_ids = set(db.session.scalars(select(ResearchSource.id).where(ResearchSource.job_id == job.id)))

    excerpt_texts: dict[int, list[str]] = {}
    for sid, text in db.session.execute(
        select(EvidenceExcerpt.source_id, EvidenceExcerpt.text)
        .join(ResearchSource)
        .where(ResearchSource.job_id == job.id)
    ):
        excerpt_texts.setdefault(sid, []).append(text)

    for section in report.sections:
        body, removed = clean_citations(section.body, job_source_ids)
        if removed:
            section.body = body
            issues.append({"type": "invalid_citation", "severity": "major", "section": section.key,
                           "detail": f"Removed {len(removed)} citation(s) to unknown sources in '{section.heading}'."})  # fmt: skip
        used = cited_ids(section.body)
        quotes = unverified_quotes(section.body, [t for sid in used for t in excerpt_texts.get(sid, [])])
        if quotes:
            issues.append({"type": "unverified_quotation", "severity": "major", "section": section.key,
                           "detail": f"A quotation in '{section.heading}' does not match any stored excerpt: “{quotes[0]}”"})  # fmt: skip
        if section.body.strip() == NOT_COVERED:
            issues.append(
                {
                    "type": "uncovered_topic",
                    "severity": "major",
                    "section": section.key,
                    "detail": f"No evidence was found for '{section.heading}', so that section is empty.",
                }
            )
        n = uncited_paragraphs(section.body)
        if n and section.body.strip() != NOT_COVERED:
            issues.append({"type": "uncited_text", "severity": "minor", "section": section.key,
                           "detail": f"{n} paragraph(s) in '{section.heading}' have no citation."})  # fmt: skip

    complete = _validate_pairs(ctx)
    claims = list(db.session.scalars(select(ResearchClaim).where(ResearchClaim.job_id == job.id)))
    counts = {"supported": 0, "partial": 0, "insufficient": 0, "contradicted": 0, "unassessed": 0}
    for claim in claims:
        claim.support_status = _claim_status(claim)
        counts[claim.support_status] += 1
    if not complete:
        issues.append({"type": "validation_incomplete", "severity": "major",
                       "detail": f"{counts['unassessed']} claim(s) could not be checked against their sources."})  # fmt: skip
    if counts["insufficient"] or counts["contradicted"]:
        issues.append({"type": "unsupported_claims", "severity": "major",
                       "detail": f"{counts['insufficient']} claim(s) were not supported by their cited excerpts and "
                                 f"{counts['contradicted']} were contradicted; check the sections that cite them."})  # fmt: skip
    if not claims:
        issues.append({"type": "no_claims", "severity": "major", "detail": "No supported claims could be extracted from the sources."})  # fmt: skip
    for src in db.session.scalars(select(ResearchSource).where(ResearchSource.job_id == job.id)):
        flagged = [w for w in (src.warnings or []) if "prompt injection" in w]
        if flagged:
            issues.append({"type": "injection_flag", "severity": "minor", "source_id": src.id,
                           "detail": f"Source S{src.id} contained text that looks like instructions to an AI; it was treated as data."})  # fmt: skip
    if ((job.checkpoint or {}).get("review") or {}).get("unavailable"):
        issues.append(
            {
                "type": "review_incomplete",
                "severity": "major",
                "detail": "The independent critic review could not run, so the draft was not reviewed.",
            }
        )
    for issue in major_issues(job):
        issues.append({"type": "critic_unresolved", "severity": "major", "section": issue["section_key"], "detail": issue["problem"]})  # fmt: skip
    job.checkpoint = {**(job.checkpoint or {}), "claim_counts": counts, "validation_complete": complete}
    db.session.commit()
    ctx.event("validation_done", f"Validation finished: {counts['supported']} supported, {counts['partial']} partial, "
              f"{counts['insufficient'] + counts['contradicted']} problem(s), {counts['unassessed']} not assessed.",
              stage="validating", agent="validator", data=counts, level="warning" if issues else "info")  # fmt: skip
    return issues


def finalize(ctx: AgentContext, issues: list[dict]) -> None:
    """Add the Limitations and References sections (generated by code, never by a model), then close the job."""
    job = ctx.job
    report = current_report(job.id)
    sources = list(
        db.session.scalars(
            select(ResearchSource).where(ResearchSource.job_id == job.id).order_by(ResearchSource.id)
        )
    )
    status_counts = {"retrieved": 0, "snippet_only": 0, "failed": 0, "blocked": 0, "paywalled": 0}
    for s in sources:
        if s.fetch_status in status_counts:
            status_counts[s.fetch_status] += 1
    counts = (job.checkpoint or {}).get("claim_counts", {})
    lines = [
        f"- {len(sources)} source(s) were collected: {status_counts['retrieved']} read in full, "
        f"{status_counts['snippet_only']} used only through a search snippet, "
        f"{status_counts['failed'] + status_counts['blocked'] + status_counts['paywalled']} could not be read.",
        f"- Claim checking: {counts.get('supported', 0)} directly supported, {counts.get('partial', 0)} partly supported, "
        f"{counts.get('insufficient', 0)} not supported, {counts.get('contradicted', 0)} contradicted, "
        f"{counts.get('unassessed', 0)} not assessed.",
        "- A reachable web page does not prove a statement. Support was judged per claim by an AI model, which can be wrong; "
        "check important statements against the cited sources.",
    ]
    lines += [f"- {i['detail']}" for i in issues if i["severity"] == "major"][:12]
    lines += [f"- Unclear in the request: {a}" for a in ((job.checkpoint or {}).get("ambiguities") or [])[:5]]
    cited: list[int] = []
    for section in report.sections:
        cited += cited_ids(section.body)
    cited = list(dict.fromkeys(cited))
    refs = []
    by_id = {s.id: s for s in sources}
    for sid in sorted(cited):
        s = by_id.get(sid)
        if s is None:
            continue
        where = s.url if s.url else f"uploaded document: {s.upload_name}"
        got = f", retrieved {s.retrieved_at.strftime('%Y-%m-%d')}" if s.retrieved_at else ""
        when = f", published {s.published_at.strftime('%Y-%m-%d')}" if s.published_at else ""
        refs.append(
            f"- [@S{sid}] {s.title or s.domain}. {s.domain or ''} {where}{when}{got}".replace("  ", " ")
        )
    from reports.models import ReportSection

    for old in [s for s in report.sections if s.key in ("limitations", "references")]:
        db.session.delete(old)
    db.session.flush()
    position = max((s.position for s in report.sections), default=-1) + 1
    db.session.add(ReportSection(report_id=report.id, key="limitations", heading="Limitations and uncertainty", level=1,
                                 position=position, body="\n".join(lines)))  # fmt: skip
    db.session.add(ReportSection(report_id=report.id, key="references", heading="References", level=1, position=position + 1,
                                 body="\n".join(refs) if refs else "No sources were cited."))  # fmt: skip
    db.session.flush()
    db.session.refresh(report)
    add_revision(
        report,
        kind="document",
        source="agent_writer",
        structure=snapshot(report),
        summary="Validated version",
    )
    major = [i for i in issues if i["severity"] == "major"]
    from reports.quality import persist

    _, evaluation = persist(report)  # stores the scores and the checked flag (gates)
    report.last_validated_at = utcnow()
    report.export_ready = not major and evaluation.gates_passed
    status = c.JOB_COMPLETED_WITH_WARNINGS if major else c.JOB_COMPLETED
    ctx.finish(status, issues=issues[:50])
    ctx.event("job_completed", "Research finished" + (" with warnings." if major else "."), stage=status, agent="system",
              level="warning" if major else "info", data={"issues": len(issues), "major": len(major)})  # fmt: skip
    db.session.commit()
