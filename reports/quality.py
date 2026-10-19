"""Quality scoring for a report.

Two kinds of evaluation are kept apart everywhere (database, screens, exports):

* ``deterministic`` - computed by code from stored data, with the formula written down in
  ``METHODS``. Reproducible. These also drive the quality gates.
* ``llm`` - an AI reader's rubric opinion on a few subjective dimensions. It is labelled as an
  opinion and never feeds a gate: an LLM score cannot prove that a statement is true.

A score of ``None`` means "not assessable" and is shown as such, never as zero or full marks.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from agents.citations import CITE, cited_ids, uncited_paragraphs, unverified_quotes
from agents.writer import NOT_COVERED
from core.db_utils import utcnow
from extensions import db
from reports.models import QualityEvaluation, ReportDocument
from research.models import (
    ClaimSource,
    EvidenceExcerpt,
    ResearchClaim,
    ResearchJob,
    ResearchSource,
    ResearchTask,
)

RUBRIC_VERSION = "1"
GENERATED = {"limitations", "references"}

METHODS = {
    "question_coverage": "Share of the research subtopics (tasks) that have at least one extracted claim.",
    "source_diversity": "60% distinct domains among cited sources divided by min(5, cited sources) plus 40% "
    "distinct categories divided by min(3, cited sources).",  # fmt: skip
    "source_relevance": "Mean search relevance score (0-1, as returned by the search provider and ranking) of "
    "the cited sources that have one, times 100.",  # fmt: skip
    "citation_integrity": "100 times the share of citation markers that point at sources of this report, "
    "minus 15 for each quotation of five or more words not found in the cited sources.",  # fmt: skip
    "evidence_coverage": "Average of (a) the share of long body paragraphs that carry a citation and (b) the "
    "share of assessed claims that are supported or partly supported by their excerpts.",  # fmt: skip
    "contradiction_handling": "Share of claims that have a contradicting source whose contradicting source is "
    "cited in the report. Not assessed when the evidence holds no contradiction.",  # fmt: skip
    "clarity_structure": "Needs a reader's judgement. See the separate AI assessment, if one was requested.",
    "completeness": "Share of planned outline sections that are present and contain written content "
    "(not the 'evidence does not cover this' placeholder).",  # fmt: skip
    "limitations": "100 when the report has a non-empty Limitations section, otherwise 0.",
    "overall": "Weighted mean of the assessed deterministic dimensions (weights in the table); not assessed "
    "dimensions are left out and the remaining weights are rescaled.",  # fmt: skip
}
WEIGHTS = {
    "citation_integrity": 25, "evidence_coverage": 20, "question_coverage": 15, "completeness": 15,
    "source_relevance": 10, "source_diversity": 10, "limitations": 5, "contradiction_handling": 5,
}  # fmt: skip
LABELS = {
    "question_coverage": "Research question coverage", "source_diversity": "Source diversity",
    "source_relevance": "Source relevance", "citation_integrity": "Citation integrity",
    "evidence_coverage": "Evidence coverage", "contradiction_handling": "Contradiction handling",
    "clarity_structure": "Clarity and structure", "completeness": "Completeness",
    "limitations": "Limitations identified", "overall": "Overall (deterministic)",
}  # fmt: skip
OPINION_DIMENSIONS = ("clarity_structure", "completeness", "question_coverage")


@dataclass
class Score:
    dimension: str
    score: float | None
    details: dict = field(default_factory=dict)
    passed_gate: bool | None = None

    @property
    def method(self) -> str:
        return METHODS[self.dimension]


@dataclass
class Gate:
    key: str
    label: str
    passed: bool
    detail: str


@dataclass
class Evaluation:
    scores: list[Score]
    gates: list[Gate]
    warnings: list[dict]

    @property
    def gates_passed(self) -> bool:
        return all(g.passed for g in self.gates)

    @property
    def overall(self) -> float | None:
        return next((s.score for s in self.scores if s.dimension == "overall"), None)

    def get(self, dimension: str) -> Score | None:
        return next((s for s in self.scores if s.dimension == dimension), None)


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _body_sections(report: ReportDocument):
    return [s for s in report.sections if s.key not in GENERATED]


# --------------------------------------------------------------------------------------- compute
def compute(report: ReportDocument) -> Evaluation:
    """Deterministic evaluation of the report as it is stored now. Writes nothing."""
    job = db.session.get(ResearchJob, report.job_id) if report.job_id else None
    job_id = job.id if job else 0
    sources = {
        s.id: s for s in db.session.scalars(select(ResearchSource).where(ResearchSource.job_id == job_id))
    }
    sections = _body_sections(report)
    warnings: list[dict] = []

    # ---- citations
    all_ids: list[int] = []
    for s in sections:
        all_ids += [int(m) for m in CITE.findall(s.body)]
    invalid = sorted({i for i in all_ids if i not in sources})
    cited = list(dict.fromkeys(i for i in all_ids if i in sources))
    excerpt_text: dict[int, list[str]] = {}
    for sid, text in db.session.execute(
        select(EvidenceExcerpt.source_id, EvidenceExcerpt.text).where(
            EvidenceExcerpt.source_id.in_(cited or [0])
        )
    ):
        excerpt_text.setdefault(sid, []).append(text)
    bad_quotes: list[dict] = []
    for s in sections:
        used = cited_ids(s.body)
        texts = [t for i in used for t in excerpt_text.get(i, [])] + [
            sources[i].title for i in used if i in sources and sources[i].title
        ]
        for q in unverified_quotes(s.body, texts):
            bad_quotes.append({"section": s.heading, "quote": q})
    ci = (
        100.0 * (len(all_ids) - sum(1 for i in all_ids if i not in sources)) / len(all_ids)
        if all_ids
        else 0.0
    )
    ci = max(0.0, round(ci - 15 * len(bad_quotes), 1))
    citation = Score("citation_integrity", ci if all_ids else None,
                     {"markers": len(all_ids), "invalid_source_ids": invalid, "unverified_quotes": bad_quotes},
                     passed_gate=not invalid and not bad_quotes)  # fmt: skip
    if not all_ids:
        citation.details["note"] = "The report contains no citations."
        citation.passed_gate = False
    for i in invalid:
        warnings.append(
            {
                "kind": "invalid_citation",
                "text": f"A citation points at source S{i}, which is not part of this report.",
            }
        )
    for q in bad_quotes:
        warnings.append(
            {
                "kind": "unverified_quote",
                "text": f"In '{q['section']}': a quotation was not found in the cited sources: “{q['quote']}”",
            }
        )

    # ---- evidence coverage
    long_paras = cited_paras = 0
    uncited_by_section: dict[str, int] = {}
    for s in sections:
        if s.body.strip() == NOT_COVERED:
            continue
        n_unc = uncited_paragraphs(s.body)
        total = sum(
            1
            for p in re.split(r"\n\s*\n", s.body)
            if len(p.strip()) >= 120 and not p.strip().startswith(("#", "|"))
        )
        long_paras += total
        cited_paras += total - n_unc
        if n_unc:
            uncited_by_section[s.heading] = n_unc
            warnings.append(
                {
                    "kind": "missing_citation",
                    "text": f"{n_unc} long paragraph(s) in '{s.heading}' have no citation.",
                }
            )
    claims = list(db.session.scalars(select(ResearchClaim).where(ResearchClaim.job_id == job_id)))
    by_status: dict[str, int] = {}
    for cl in claims:
        by_status[cl.support_status] = by_status.get(cl.support_status, 0) + 1
    assessed = len(claims) - by_status.get("unassessed", 0)
    parts = []
    if long_paras:
        parts.append(_pct(cited_paras, long_paras))
    if assessed:
        parts.append(_pct(by_status.get("supported", 0) + by_status.get("partial", 0), assessed))
    weak = by_status.get("insufficient", 0) + by_status.get("contradicted", 0)
    evidence = Score("evidence_coverage", round(sum(parts) / len(parts), 1) if parts else None,
                     {"paragraphs_with_citation": cited_paras, "long_paragraphs": long_paras, "claim_status": by_status,
                      "uncited_by_section": uncited_by_section},
                     passed_gate=weak == 0)  # fmt: skip
    for cl in [c for c in claims if c.support_status in ("insufficient", "contradicted")][:10]:
        warnings.append(
            {
                "kind": "weak_claim",
                "text": f"Claim not supported by its cited excerpt ({cl.support_status}): {cl.text[:200]}",
            }
        )
    if by_status.get("unassessed") and claims:
        warnings.append(
            {
                "kind": "unassessed_claims",
                "text": f"{by_status['unassessed']} claim(s) were never checked against their sources.",
            }
        )

    # ---- coverage / completeness
    tasks = list(db.session.scalars(select(ResearchTask).where(ResearchTask.job_id == job_id)))
    with_claims = set(
        db.session.scalars(
            select(ResearchClaim.task_id).where(
                ResearchClaim.job_id == job_id, ResearchClaim.task_id.is_not(None)
            )
        )
    )
    qc = Score("question_coverage", _pct(sum(1 for t in tasks if t.id in with_claims), len(tasks)) if tasks else None,
               {"subtopics": len(tasks), "without_claims": [t.title for t in tasks if t.id not in with_claims]})  # fmt: skip
    for t in qc.details["without_claims"]:
        warnings.append(
            {"kind": "uncovered_question", "text": f"No claims were extracted for the subtopic '{t}'."}
        )
    outline = (job.checkpoint or {}).get("outline") or [] if job else []
    present = {s.key: s for s in sections}
    missing = [o["heading"] for o in outline if o["key"] not in present]
    empty = [
        o["heading"]
        for o in outline
        if o["key"] in present
        and (not present[o["key"]].body.strip() or present[o["key"]].body.strip() == NOT_COVERED)
    ]
    comp = Score("completeness", _pct(len(outline) - len(missing) - len(empty), len(outline)) if outline else None,
                 {"planned_sections": len(outline), "missing": missing, "empty_or_uncovered": empty},
                 passed_gate=not missing and not empty)  # fmt: skip
    for h in missing:
        warnings.append({"kind": "missing_section", "text": f"The planned section '{h}' is missing."})
    for h in empty:
        warnings.append(
            {
                "kind": "empty_section",
                "text": f"The section '{h}' is empty or says the evidence does not cover it.",
            }
        )

    # ---- sources
    cited_sources = [sources[i] for i in cited]
    if cited_sources:
        n = len(cited_sources)
        dom = len({s.domain for s in cited_sources if s.domain}) / min(5, n)
        cat = len({s.category for s in cited_sources}) / min(3, n)
        div = round(100 * (0.6 * min(1.0, dom) + 0.4 * min(1.0, cat)), 1)
    else:
        div = None
    diversity = Score("source_diversity", div, {"cited_sources": len(cited_sources),
                      "domains": sorted({s.domain for s in cited_sources if s.domain})})  # fmt: skip
    rel = [s.relevance_score for s in cited_sources if s.relevance_score is not None]
    relevance = Score("source_relevance", round(100 * sum(rel) / len(rel), 1) if rel else None,
                      {"scored_sources": len(rel), "cited_sources": len(cited_sources)})  # fmt: skip
    for s in cited_sources:
        if s.fetch_status in ("failed", "blocked", "paywalled"):
            warnings.append(
                {
                    "kind": "unavailable_source",
                    "text": f"Cited source S{s.id} ({s.domain or s.upload_name}) could not be retrieved ({s.fetch_status}).",
                }
            )
        elif s.fetch_status == "snippet_only":
            warnings.append(
                {
                    "kind": "snippet_source",
                    "text": f"Cited source S{s.id} ({s.domain or s.upload_name}) was used through a search snippet only.",
                }
            )

    # ---- contradictions
    links = list(
        db.session.scalars(
            select(ClaimSource).where(ClaimSource.job_id == job_id, ClaimSource.relation == "contradicts")
        )
    )
    if links:
        handled = sum(1 for lk in links if lk.source_id in cited)
        contra = Score(
            "contradiction_handling",
            _pct(handled, len(links)),
            {"contradicting_links": len(links), "cited": handled},
        )
        if handled < len(links):
            warnings.append(
                {
                    "kind": "contradiction",
                    "text": f"{len(links) - handled} contradicting finding(s) are not cited in the report.",
                }
            )
        else:
            warnings.append(
                {
                    "kind": "contradiction_note",
                    "text": f"The evidence contains {len(links)} contradicting finding(s); read how the report treats them.",
                }
            )
    else:
        contra = Score(
            "contradiction_handling",
            None,
            {"note": "The collected evidence contains no contradicting finding."},
        )

    # ---- limitations
    lim = next((s for s in report.sections if s.key == "limitations"), None)
    limitations = Score("limitations", 100.0 if lim and lim.body.strip() else 0.0, {"present": bool(lim and lim.body.strip())},
                        passed_gate=bool(lim and lim.body.strip()))  # fmt: skip
    if not limitations.passed_gate:
        warnings.append({"kind": "no_limitations", "text": "The report has no Limitations section."})

    clarity = Score("clarity_structure", None, {"note": "Not measured by code."})
    dims = [qc, diversity, relevance, citation, evidence, contra, clarity, comp, limitations]
    scored = [(WEIGHTS[s.dimension], s.score) for s in dims if s.score is not None and s.dimension in WEIGHTS]
    total_w = sum(w for w, _ in scored)
    overall = Score("overall", round(sum(w * v for w, v in scored) / total_w, 1) if total_w else None,
                    {"weights": {k: v for k, v in WEIGHTS.items()}, "assessed": [s.dimension for s in dims if s.score is not None and s.dimension in WEIGHTS]})  # fmt: skip

    # ---- gates
    check = (job.checkpoint or {}) if job else {}
    review_unavailable = bool((check.get("review") or {}).get("unavailable"))
    edited = {s.key for s in sections if s.edited_by_user}
    open_review = [
        i
        for i in ((check.get("review") or {}).get("issues") or [])
        if i["severity"] == "major" and i["section_key"] not in edited
    ]
    gates = [
        Gate("citations", "Every citation points at a source of this report and no quotation is unverified",
             bool(citation.passed_gate), "" if citation.passed_gate else "See citation warnings."),
        Gate("claims", "No claim is unsupported or contradicted by its cited excerpt", weak == 0,
             f"{weak} claim(s) affected." if weak else ""),
        Gate("claim_check", "Claim support checking finished", bool(check.get("validation_complete", False)) or not claims,
             "" if check.get("validation_complete", False) or not claims else "Some claims were never checked."),
        Gate("completeness", "Every planned section is present and has content", bool(comp.passed_gate),
             ", ".join(missing + empty)[:200]),
        Gate("limitations", "A Limitations section is present", bool(limitations.passed_gate), ""),
        Gate("review", "The independent review ran and left no major issue open", not review_unavailable and not open_review,
             "The review could not run." if review_unavailable else f"{len(open_review)} major issue(s) still open." if open_review else ""),
    ]  # fmt: skip
    for key in edited:
        warnings.append(
            {
                "kind": "edited",
                "text": f"The section '{present[key].heading}' was edited by you after the AI review; the review did not re-read it.",
            }
        )
    for g in gates:
        if not g.passed:
            warnings.append(
                {
                    "kind": "gate_failed",
                    "text": f"Quality gate failed: {g.label}." + (f" {g.detail}" if g.detail else ""),
                }
            )
    overall.passed_gate = all(g.passed for g in gates)
    return Evaluation(dims + [overall], gates, warnings)


# --------------------------------------------------------------------------------------- persist
def persist(report: ReportDocument, evaluation: Evaluation | None = None) -> tuple[str, Evaluation]:
    """Store a deterministic evaluation, update the report's checked flag, and return (run_id, evaluation).
    The caller commits."""
    evaluation = evaluation or compute(report)
    run_id = uuid.uuid4().hex
    for s in evaluation.scores:
        details = dict(s.details)
        if s.dimension == "overall":
            details["gates"] = [
                {"key": g.key, "label": g.label, "passed": g.passed, "detail": g.detail}
                for g in evaluation.gates
            ]
            details["warnings"] = evaluation.warnings[:60]
        db.session.add(QualityEvaluation(
            report_id=report.id, run_id=run_id, revision_no=report.current_revision_no, evaluator="deterministic",
            dimension=s.dimension, status="scored" if s.score is not None else "not_assessed", score=s.score,
            rubric_version=RUBRIC_VERSION, method=s.method[:500], passed_gate=s.passed_gate, details=details,
        ))  # fmt: skip
    report.export_ready = evaluation.gates_passed
    report.last_validated_at = utcnow()
    db.session.flush()
    return run_id, evaluation


def latest_rows(report: ReportDocument, evaluator: str) -> list[QualityEvaluation]:
    run = db.session.scalar(
        select(QualityEvaluation.run_id).where(QualityEvaluation.report_id == report.id, QualityEvaluation.evaluator == evaluator)
        .order_by(QualityEvaluation.id.desc()).limit(1)
    )  # fmt: skip
    if run is None:
        return []
    return list(db.session.scalars(
        select(QualityEvaluation).where(QualityEvaluation.report_id == report.id, QualityEvaluation.run_id == run,
                                        QualityEvaluation.evaluator == evaluator).order_by(QualityEvaluation.id)
    ))  # fmt: skip


def assess_with_llm(report: ReportDocument, user) -> list[QualityEvaluation]:
    """Ask a model for a rubric opinion on the subjective dimensions and store it as ``llm`` rows."""
    from agents import prompts
    from agents.schemas import QualityOpinion
    from reports.editing import EditError, _llm, run_is_active
    from reports.render import to_plain_text
    from research_tools.untrusted import wrap_untrusted

    if run_is_active(report):
        raise EditError("busy", "The run that wrote this report is still active.", 409)
    job = db.session.get(ResearchJob, report.job_id) if report.job_id else None
    objective = ((job.checkpoint or {}).get("objective") if job else None) or report.title
    text = to_plain_text(report, {})[:24000]
    prompt = f"Research objective: {objective}\n\n" + wrap_untrusted(text, label="report")
    result = _llm("critic", prompts.QUALITY_JUDGE, prompt, QualityOpinion, user, report, 1200)
    run_id = uuid.uuid4().hex
    rows = []
    seen = set()
    for op in result.opinions:
        if op.dimension in seen:
            continue
        seen.add(op.dimension)
        rows.append(QualityEvaluation(
            report_id=report.id, run_id=run_id, revision_no=report.current_revision_no, evaluator="llm",
            dimension=op.dimension, status="scored", score=float(op.score), rubric_version=RUBRIC_VERSION,
            method="An AI model's opinion against a written rubric. It cannot verify facts.",
            details={"rationale": op.rationale},
        ))  # fmt: skip
    if not rows:
        raise EditError("llm_bad_output", "The AI assessment returned no scores.", 502)
    db.session.add_all(rows)
    db.session.flush()
    return rows
