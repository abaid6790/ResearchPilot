"""Editing a report: saving sections, history, regeneration proposals, critiques.

Rules this module enforces:
- A save needs the section version the editor loaded; a stale version is a conflict, never an overwrite.
- Regenerated text is only ever stored as a *proposed* revision. The user applies or discards it.
- Every applied change is a revision, so nothing is lost; restoring creates a new revision.
- After any edit the report is no longer "export ready": the automatic checks ran on the earlier text.
- Citations are re-checked after each save (markers must point at sources of this report's run).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from sqlalchemy import select, update

from agents import prompts
from agents.citations import CITE, cited_ids, clean_citations, uncited_paragraphs, unverified_quotes
from agents.schemas import SectionCritique, SectionDraft
from agents.writer import NOT_COVERED, claims_for_section, format_claims
from extensions import db
from providers.base import LLMRequest
from reports.models import ReportDocument, ReportRevision, ReportSection
from research.constants import JOB_ACTIVE_STATUSES
from research.models import EvidenceExcerpt, ResearchJob, ResearchSource
from research_tools.untrusted import wrap_untrusted

MAX_BODY = 20000
AUTOSAVE_NOTE = "Autosave"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class EditError(Exception):
    """A refused edit. ``code`` is machine-readable, ``message`` is safe to show."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def clean_body(text: str) -> str:
    text = _CONTROL.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    if len(text) > MAX_BODY:
        raise EditError("too_long", f"A section can hold at most {MAX_BODY} characters.", 422)
    return text.strip("\n")


# ------------------------------------------------------------------------------ lookups
def find_section(report: ReportDocument, key: str) -> ReportSection:
    for section in report.sections:
        if section.key == key:
            return section
    raise EditError("not_found", "No such section.", 404)


def run_is_active(report: ReportDocument) -> bool:
    if not report.job_id:
        return False
    status = db.session.scalar(select(ResearchJob.status).where(ResearchJob.id == report.job_id))
    return status in JOB_ACTIVE_STATUSES


def _guard(report: ReportDocument, section: ReportSection) -> None:
    if section.key in {"references"}:
        raise EditError(
            "locked", "The references list is generated from your citations and cannot be edited.", 403
        )
    if run_is_active(report):
        raise EditError(
            "busy", "The run that wrote this report is still active. Edit after it finishes.", 409
        )


def job_source_ids(report: ReportDocument) -> set[int]:
    if not report.job_id:
        return set()
    return set(db.session.scalars(select(ResearchSource.id).where(ResearchSource.job_id == report.job_id)))


# ------------------------------------------------------------------------------ checks
@dataclass
class Checks:
    invalid_citations: list[int] = field(default_factory=list)
    unverified_quotes: list[str] = field(default_factory=list)
    uncited_paragraphs: int = 0

    @property
    def ok(self) -> bool:
        return not (self.invalid_citations or self.unverified_quotes or self.uncited_paragraphs)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "invalid_citations": self.invalid_citations,
                "unverified_quotes": self.unverified_quotes, "uncited_paragraphs": self.uncited_paragraphs}  # fmt: skip


def check_body(report: ReportDocument, body: str) -> Checks:
    """The same deterministic checks the pipeline uses. They find broken citations, quotations that
    are not in the cited sources and long uncited paragraphs. They cannot prove a claim is true."""
    ids = cited_ids(body)
    valid = job_source_ids(report)
    bad = [i for i in ids if i not in valid]
    texts: list[str] = []
    if ids:
        texts = list(
            db.session.scalars(select(EvidenceExcerpt.text).where(EvidenceExcerpt.source_id.in_(ids or [0])))
        ) + [
            t for t in db.session.scalars(select(ResearchSource.title).where(ResearchSource.id.in_(ids))) if t
        ]
    return Checks(bad, unverified_quotes(body, texts), uncited_paragraphs(body))


# ------------------------------------------------------------------------------ revisions
def _next_revision_no(report: ReportDocument) -> int:
    report.current_revision_no += 1
    return report.current_revision_no


def _revision(report, section, *, source, status, content, summary, user, based_on=None, structure=None):
    rev = ReportRevision(
        report_id=report.id, revision_no=_next_revision_no(report), kind="section", section_id=section.id,
        section_key=section.key, source=source, status=status, heading=section.heading, content=content,
        structure=structure, change_summary=summary, author_user_id=user.id, based_on_revision_id=based_on,
    )  # fmt: skip
    db.session.add(rev)
    return rev


def latest_applied(report: ReportDocument, section: ReportSection) -> ReportRevision | None:
    return db.session.scalars(
        select(ReportRevision)
        .where(ReportRevision.report_id == report.id, ReportRevision.section_key == section.key,
               ReportRevision.status == "applied")
        .order_by(ReportRevision.revision_no.desc())
    ).first()  # fmt: skip


def _claim_version(section: ReportSection, expected_version: int) -> None:
    """Atomic compare-and-set on the section version, so two tabs cannot silently overwrite each other."""
    changed = db.session.execute(
        update(ReportSection)
        .where(ReportSection.id == section.id, ReportSection.version == expected_version)
        .values(version=expected_version + 1)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not changed:
        db.session.rollback()
        raise EditError("conflict", "This section was changed somewhere else. Reload before saving.", 409)
    db.session.refresh(section)


def save_section(report: ReportDocument, section: ReportSection, body: str, expected_version: int, user,
                 *, autosave: bool = False) -> tuple[ReportSection, Checks]:  # fmt: skip
    _guard(report, section)
    body = clean_body(body)
    if body == section.body:
        return section, check_body(report, body)
    _claim_version(section, expected_version)
    last = latest_applied(report, section)
    if autosave and last and last.source == "user_edit" and last.change_summary == AUTOSAVE_NOTE:
        last.content = body  # keep one rolling autosave entry instead of flooding the history
    else:
        _revision(report, section, source="user_edit", status="applied", content=body, user=user,
                  summary=AUTOSAVE_NOTE if autosave else "Saved by you",
                  based_on=last.id if last else None)  # fmt: skip
    section.body = body
    section.edited_by_user = True
    report.export_ready = False
    report.version += 1
    db.session.commit()
    return section, check_body(report, body)


def restore_revision(report: ReportDocument, section: ReportSection, revision: ReportRevision,
                     expected_version: int, user) -> ReportSection:  # fmt: skip
    _guard(report, section)
    if revision.report_id != report.id or revision.section_key != section.key or revision.status != "applied":
        raise EditError("not_found", "That version cannot be restored.", 404)
    if revision.content is None:
        raise EditError("not_found", "That version has no text.", 404)
    _claim_version(section, expected_version)
    _revision(report, section, source="restore", status="applied", content=revision.content, user=user,
              summary=f"Restored version {revision.revision_no}", based_on=revision.id)  # fmt: skip
    section.body = revision.content
    section.edited_by_user = True
    report.export_ready = False
    report.version += 1
    db.session.commit()
    return section


# ------------------------------------------------------------------------------ regeneration
def _router():
    from flask import current_app

    from providers import get_router

    return get_router(current_app)


def _llm(task: str, system: str, prompt: str, model, user, report: ReportDocument, max_tokens: int):
    from providers.base import LLMRequestError, LLMUnavailableError, NoProviderConfiguredError

    request = LLMRequest.simple(prompt, system=system, task=task, response_model=model,
                                max_output_tokens=max_tokens, job_id=report.job_id, user_id=user.id)  # fmt: skip
    try:
        response = _router().generate(request)
    except NoProviderConfiguredError as exc:
        raise EditError("llm_not_configured", "No AI provider keys are configured.", 503) from exc
    except LLMUnavailableError as exc:
        raise EditError(
            "llm_unavailable", "No AI provider could answer right now. Try again later.", 503
        ) from exc
    except LLMRequestError as exc:
        raise EditError("llm_rejected", "The AI provider rejected the request.", 502) from exc
    if not isinstance(response.parsed, model):
        raise EditError("llm_bad_output", "The AI provider returned output that could not be used.", 502)
    return response.parsed


def _section_claims(report: ReportDocument, section: ReportSection):
    job = db.session.get(ResearchJob, report.job_id) if report.job_id else None
    if job is None:
        raise EditError("no_evidence", "The evidence for this report is no longer stored.", 409)
    outline = {o["key"]: o for o in (job.checkpoint or {}).get("outline") or []}
    claims = claims_for_section(job.id, (outline.get(section.key) or {}).get("subtopics") or [])
    return claims


def propose_regeneration(
    report: ReportDocument, section: ReportSection, instruction: str, user
) -> ReportRevision:
    _guard(report, section)
    instruction = " ".join((instruction or "").split())[:300]
    claims = _section_claims(report, section)
    if not claims:
        raise EditError("no_evidence", "There are no collected claims for this section to write from.", 409)
    block, allowed = format_claims(claims)
    prompt = (
        f"Section: {section.heading}\nCurrent text (may contain the user's own edits):\n"
        + wrap_untrusted(section.body, label="current_text")
        + (f"\n\nThe user asks for: {instruction}\n" if instruction else "\n\nRewrite it clearly.\n")
        + "\n"
        + wrap_untrusted(block, label="claims")
    )
    draft = _llm("writer", prompts.REVISER, prompt, SectionDraft, user, report, 3000)
    body, removed = clean_citations(draft.body.strip(), allowed)
    if not body or body == NOT_COVERED:
        raise EditError("empty", "The model did not produce a usable section.", 502)
    last = latest_applied(report, section)
    summary = "Suggested rewrite" + (f": {instruction[:120]}" if instruction else "")
    rev = _revision(report, section, source="regeneration", status="proposed", content=body, user=user,
                    summary=summary, based_on=last.id if last else None,
                    structure={"section_version": section.version, "removed_citations": removed})  # fmt: skip
    db.session.commit()
    return rev


def pending_proposals(report: ReportDocument, section: ReportSection) -> list[ReportRevision]:
    return list(
        db.session.scalars(
            select(ReportRevision)
            .where(ReportRevision.report_id == report.id, ReportRevision.section_key == section.key,
                   ReportRevision.status == "proposed")
            .order_by(ReportRevision.revision_no.desc())
        )
    )  # fmt: skip


def apply_proposal(
    report: ReportDocument, section: ReportSection, proposal: ReportRevision, user
) -> ReportSection:
    _guard(report, section)
    if (
        proposal.report_id != report.id
        or proposal.section_key != section.key
        or proposal.status != "proposed"
    ):
        raise EditError("not_found", "That suggestion is no longer available.", 404)
    seen = (proposal.structure or {}).get("section_version")
    if seen != section.version:
        raise EditError(
            "stale", "The section changed after this suggestion was made. Discard it and ask again.", 409
        )
    _claim_version(section, section.version)
    proposal.status = "applied"
    proposal.author_user_id = user.id
    section.body = proposal.content or ""
    report.export_ready = False
    report.version += 1
    db.session.commit()
    return section


def discard_proposal(report: ReportDocument, section: ReportSection, proposal: ReportRevision) -> None:
    if (
        proposal.report_id != report.id
        or proposal.section_key != section.key
        or proposal.status != "proposed"
    ):
        raise EditError("not_found", "That suggestion is no longer available.", 404)
    proposal.status = "discarded"
    db.session.commit()


def critique_section(report: ReportDocument, section: ReportSection, user) -> SectionCritique:
    if run_is_active(report):
        raise EditError("busy", "The run that wrote this report is still active.", 409)
    try:
        claims = _section_claims(report, section)
    except EditError:
        claims = []
    block = format_claims(claims)[0] if claims else "(no claims stored for this section)"
    prompt = (f"Section: {section.heading}\n" + wrap_untrusted(section.body, label="section_text") + "\n\n"
              + wrap_untrusted(block, label="claims"))  # fmt: skip
    return _llm("critic", prompts.SECTION_CRITIC, prompt, SectionCritique, user, report, 1500)


# ------------------------------------------------------------------------------ comparing
def diff_lines(old: str, new: str) -> list[dict]:
    """Line diff for display: [{'op': 'same'|'add'|'del', 'text': str}]. Text is plain; templates escape it."""
    a, b = (old or "").split("\n"), (new or "").split("\n")
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            out += [{"op": "same", "text": t} for t in a[i1:i2]]
        else:
            out += [{"op": "del", "text": t} for t in a[i1:i2]]
            out += [{"op": "add", "text": t} for t in b[j1:j2]]
    return out


def section_citation_ids(body: str) -> list[int]:
    return [int(m) for m in dict.fromkeys(CITE.findall(body))]
