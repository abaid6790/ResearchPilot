"""The research workflow end to end, with a scripted LLM, a fake search client and a fake web."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agents import citations
from agents.context import AgentContext
from agents.runner import run_job
from agents.schemas import (
    ClaimEvidence,
    CriticReview,
    FindingClaim,
    PairVerdict,
    SectionDraft,
    SubtopicFindings,
    ValidationBatch,
)  # fmt: skip
from extensions import db
from providers.base import ErrorKind
from reports.models import ReportDocument
from research.models import (
    ClaimSource,
    ResearchClaim,
    ResearchSource,
    ResearchTask,
    ResearchUpload,
)
from research.services import create_job, create_project
from research_tools.fetch import FetchSettings, SafeFetcher
from research_tools.search import SearchUnavailableError
from tests.conftest import make_user
from tests.fake_llm import FakeLLM, FakeSearch, major_issue
from tests.fake_web import FakeWeb


@pytest.fixture()
def web():
    with FakeWeb() as w:
        yield w


@pytest.fixture()
def alice(app):
    return make_user("alice@example.com", name="Alice")


def make_ctx(web, job, llm, search=None, **kw):
    urls = [web.url(f"/page/{i}") for i in range(1, 11)]
    search = search or FakeSearch(urls)
    factory = lambda: SafeFetcher(  # noqa: E731
        FetchSettings(),
        resolver=lambda h, p: ["127.0.0.1"],
        ip_ok=lambda ip: True,
        ports=frozenset({web.port}),
    )
    return AgentContext(job_id=job.id, router=llm, search=search, fetcher_factory=factory, **kw)


def new_job(alice, **settings):
    project = create_project(
        alice, title="Solar", topic="How efficient are solar panels today?", settings=settings
    )
    return create_job(project)


def report_of(job):
    return db.session.scalars(select(ReportDocument).where(ReportDocument.job_id == job.id)).one()


def section_bodies(job):
    return {s.key: s.body for s in report_of(job).sections}


# ------------------------------------------------------------------ happy path
def test_full_run_produces_cited_validated_report(app, alice, web):
    job = new_job(alice, depth="quick")
    llm = FakeLLM()
    run_job(make_ctx(web, job, llm))
    db.session.refresh(job)
    assert job.status == "completed", (job.status, job.error_message, job.unresolved_issues)
    assert job.started_at and job.finished_at and job.error_code is None

    tasks = db.session.scalars(select(ResearchTask).where(ResearchTask.job_id == job.id)).all()
    assert len(tasks) == 2 and all(t.status == "done" and t.result_summary for t in tasks)
    sources = db.session.scalars(select(ResearchSource).where(ResearchSource.job_id == job.id)).all()
    assert sources and any(s.fetch_status == "retrieved" for s in sources)
    assert all(s.link_status in ("reachable", "unchecked", "redirected") for s in sources)

    claims = db.session.scalars(select(ResearchClaim).where(ResearchClaim.job_id == job.id)).all()
    assert claims and all(c.support_status == "supported" for c in claims)
    links = db.session.scalars(select(ClaimSource).where(ClaimSource.job_id == job.id)).all()
    assert links and all(link.support_level == "direct" and link.excerpt_id for link in links)

    report = report_of(job)
    keys = [s.key for s in report.sections]
    assert keys == ["overview", "temperature", "limitations", "references"]
    assert report.export_ready and report.last_validated_at and report.current_revision_no >= 2
    bodies = section_bodies(job)
    cited = citations.cited_ids(bodies["overview"])
    assert cited and all(i in {s.id for s in sources} for i in cited)
    for sid in cited:  # every cited source appears in the references with its URL
        assert f"[@S{sid}]" in bodies["references"]
    assert "directly supported" in bodies["limitations"] and "Which region is meant?" in bodies["limitations"]
    from reports.models import QualityEvaluation

    rows = db.session.scalars(select(QualityEvaluation).where(QualityEvaluation.report_id == report.id)).all()
    assert rows and {r.evaluator for r in rows} == {"deterministic"}
    assert next(r for r in rows if r.dimension == "overall").passed_gate is True
    events = [e.event_type for e in job.events]
    assert events[0] == "job_queued" and events[-1] == "job_completed" and "plan_ready" in events
    assert [e.seq for e in job.events] == sorted(e.seq for e in job.events)


def test_finished_jobs_are_not_run_again(app, alice, web):
    job = new_job(alice, depth="quick")
    llm = FakeLLM()
    run_job(make_ctx(web, job, llm))
    calls = len(llm.calls)
    run_job(make_ctx(web, job, llm))
    assert len(llm.calls) == calls


def test_source_cap_is_respected(app, alice, web):
    job = new_job(alice, depth="quick", max_sources=3)
    run_job(make_ctx(web, job, FakeLLM()))
    assert (
        db.session.scalar(
            select(db.func.count()).select_from(ResearchSource).where(ResearchSource.job_id == job.id)
        )
        <= 3
    )


# ------------------------------------------------------------------ untrusted content
def test_page_text_is_wrapped_as_untrusted_and_injection_is_flagged(app, alice, web):
    job = new_job(alice, depth="quick")
    llm = FakeLLM()
    run_job(make_ctx(web, job, llm))
    analyst_prompts = [p for t, _, p in llm.calls if t == "analyst"]
    assert analyst_prompts and all("<untrusted_content" in p for p in analyst_prompts)
    assert any("Ignore all previous instructions" in p for p in analyst_prompts)  # kept as evidence...
    systems = {s for t, s, _ in llm.calls if t == "analyst"}
    assert all("never instructions" in s for s in systems)  # ...but the model is told it is data
    sources = db.session.scalars(select(ResearchSource).where(ResearchSource.job_id == job.id)).all()
    assert any(any("prompt injection" in w for w in (s.warnings or [])) for s in sources)
    db.session.refresh(job)
    assert any(i["type"] == "injection_flag" for i in job.unresolved_issues or [])


def test_model_cannot_cite_excerpts_or_sources_it_was_not_given(app, alice, web):
    def analyst(request, llm):
        return SubtopicFindings(claims=[
            FindingClaim(text="A claim citing an excerpt that does not exist at all.", confidence=0.9,
                         evidence=[ClaimEvidence(excerpt_id="E999999")]),
            FindingClaim(text="Another claim also citing nothing real to support it.", confidence=0.9,
                         evidence=[ClaimEvidence(excerpt_id="not-an-id")]),
        ], summary="x", sufficient=True)  # fmt: skip

    def writer(request, llm):
        return SectionDraft(body="Panels are great [@S987654]. " + "Filler text here. " * 10)

    job = new_job(alice, depth="quick", enable_additional_research=False)
    run_job(make_ctx(web, job, FakeLLM(analyst=analyst, writer=writer)))
    db.session.refresh(job)
    assert (
        db.session.scalar(select(db.func.count()).select_from(ResearchClaim)) == 0
    )  # fabricated evidence is dropped
    assert job.status == "completed_with_warnings"
    assert any(i["type"] == "no_claims" for i in job.unresolved_issues)
    assert "S987654" not in " ".join(section_bodies(job).values())
    assert not report_of(job).export_ready


def test_fabricated_quotation_is_flagged(app, alice, web):
    def writer(request, llm):
        return SectionDraft(
            body='The lab said "panels will reach ninety nine percent efficiency by next year" [@S1]. '
            + "More. " * 20
        )

    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, FakeLLM(writer=writer)))
    db.session.refresh(job)
    assert job.status == "completed_with_warnings"
    assert any(i["type"] == "unverified_quotation" for i in job.unresolved_issues)


def test_unsupported_claims_are_flagged_not_hidden(app, alice, web):
    def validator(request, llm):
        import re

        ids = re.findall(r"\[P(\d+)\]", request.messages[0].content)
        return ValidationBatch(verdicts=[PairVerdict(pair_id=f"P{i}", support="unrelated") for i in ids])

    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, FakeLLM(validator=validator)))
    db.session.refresh(job)
    assert job.status == "completed_with_warnings" and not report_of(job).export_ready
    assert {c.support_status for c in db.session.scalars(select(ResearchClaim))} == {"insufficient"}
    assert "not supported" in section_bodies(job)["limitations"]


def test_validator_outage_marks_claims_not_assessed(app, alice, web):
    llm = FakeLLM()
    llm.fail_tasks = {"validator"}
    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, llm))
    db.session.refresh(job)
    assert job.status == "completed_with_warnings"
    assert {c.support_status for c in db.session.scalars(select(ResearchClaim))} == {"unassessed"}
    assert any(i["type"] == "validation_incomplete" for i in job.unresolved_issues)


# ------------------------------------------------------------------ critic and revision
def test_major_issue_triggers_revision_then_passes(app, alice, web):
    state = {"n": 0}

    def critic(request, llm):
        state["n"] += 1
        return CriticReview(issues=[major_issue("overview")] if state["n"] == 1 else [])

    llm = FakeLLM(critic=critic)
    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, llm))
    db.session.refresh(job)
    assert job.status == "completed" and job.revision_round == 1
    assert llm.count("critic") == 2
    assert any(r.source == "agent_editor" for r in report_of(job).revisions)


def test_unresolved_major_issue_after_revision_limit_is_reported(app, alice, web):
    llm = FakeLLM(critic=lambda _r, _l: CriticReview(issues=[major_issue("overview")]))
    job = new_job(alice, depth="quick", max_revision_rounds=1)
    run_job(make_ctx(web, job, llm))
    db.session.refresh(job)
    assert llm.count("critic") == 2 and job.revision_round == 1
    assert job.status == "completed_with_warnings"
    assert any(i["type"] == "critic_unresolved" for i in job.unresolved_issues)


def test_user_edited_sections_are_never_rewritten(app, alice, web):
    llm = FakeLLM(critic=lambda _r, _l: CriticReview(issues=[major_issue("overview")]))
    job = new_job(alice, depth="quick", max_revision_rounds=1)
    ctx = make_ctx(web, job, llm)
    # pre-run to create the report, then mark the section as user-edited and revise
    from agents.analyst import run_analysis
    from agents.planner import run_planner
    from agents.researcher import run_research
    from agents.review import run_critic, run_revision
    from agents.writer import run_writer

    for step in (run_planner, run_research, run_analysis, run_writer, run_critic):
        step(ctx)
    section = report_of(job).sections[0]
    section.edited_by_user, section.body = True, "My own words."
    db.session.commit()
    writer_calls = llm.count("writer")
    run_revision(ctx)
    db.session.refresh(section)
    assert section.body == "My own words." and llm.count("writer") == writer_calls


def test_critic_outage_is_disclosed(app, alice, web):
    llm = FakeLLM()
    llm.fail_tasks = {"critic"}
    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, llm))
    db.session.refresh(job)
    assert job.status == "completed_with_warnings"
    assert any(i["type"] == "review_incomplete" for i in job.unresolved_issues)


def test_critic_can_be_turned_off(app, alice, web):
    llm = FakeLLM()
    job = new_job(alice, depth="quick", enable_critic=False)
    run_job(make_ctx(web, job, llm))
    assert llm.count("critic") == 0


# ------------------------------------------------------------------ follow-up research
def test_thin_evidence_triggers_one_more_round(app, alice, web):
    llm = FakeLLM()
    llm.sufficient_after = 2  # first pass over both subtopics says "not enough"
    job = new_job(alice, depth="standard")
    search = FakeSearch([web.url(f"/page/{i}") for i in range(1, 11)])
    run_job(make_ctx(web, job, llm, search))
    db.session.refresh(job)
    assert job.research_round == 2 and job.status == "completed"
    assert llm.count("planner") == 3  # plan + two follow-ups
    assert any("temperature coefficient" in q for q in search.queries)


def test_follow_up_rounds_are_bounded(app, alice, web):
    llm = FakeLLM()
    llm.sufficient_after = 10_000  # never sufficient
    job = new_job(alice, depth="quick", max_research_rounds=1)
    run_job(make_ctx(web, job, llm))
    db.session.refresh(job)
    assert job.research_round == 1


# ------------------------------------------------------------------ failures, resume, cancel, limits
def test_provider_outage_fails_cleanly_and_resumes_without_repeating_work(app, alice, web):
    llm = FakeLLM()
    llm.fail_tasks = {"analyst"}
    job = new_job(alice, depth="quick")
    search = FakeSearch([web.url(f"/page/{i}") for i in range(1, 11)])
    run_job(make_ctx(web, job, llm, search))
    db.session.refresh(job)
    assert job.status == "failed" and job.error_code == "llm_unavailable"
    assert "try again later" in job.error_message and job.finished_at
    queries_before, plans = len(search.queries), llm.count("planner")
    # the active-job index is released: a new job could start; and we resume this one
    job.status, job.finished_at = "queued", None
    db.session.commit()
    llm.fail_tasks = set()
    run_job(make_ctx(web, job, llm, search))
    db.session.refresh(job)
    assert job.status == "completed"
    assert (
        llm.count("planner") == plans and len(search.queries) == queries_before
    )  # no re-planning, no re-searching


def test_cancel_request_stops_the_run(app, alice, web):
    from core.db_utils import utcnow

    job = new_job(alice, depth="quick")
    job.cancel_requested_at = utcnow()
    db.session.commit()
    llm = FakeLLM()
    run_job(make_ctx(web, job, llm))
    db.session.refresh(job)
    assert job.status == "cancelled" and llm.calls == []


def test_llm_call_budget_stops_the_run(app, alice, web):
    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, FakeLLM(), max_llm_calls=2))
    db.session.refresh(job)
    assert job.status == "failed" and job.error_code == "budget_exceeded"


def test_missing_search_key_fails_with_clear_reason(app, alice, web):
    search = FakeSearch([], error=SearchUnavailableError("not configured", ErrorKind.AUTH_INVALID))
    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, FakeLLM(), search))
    db.session.refresh(job)
    assert job.status == "failed" and job.error_code == "search_not_configured"


def test_uploads_alone_can_carry_a_run_when_search_is_down(app, alice, web):
    project = create_project(
        alice,
        title="Notes",
        topic="What do my notes say about solar efficiency?",
        settings={"depth": "quick"},
    )
    db.session.add(ResearchUpload(project_id=project.id, filename="notes.txt", kind="txt", size_bytes=300, sha256="c" * 64,
                                  text="Solar panel efficiency is about 22 percent in modern modules. " * 6))  # fmt: skip
    db.session.commit()
    job = create_job(project)
    search = FakeSearch([], error=SearchUnavailableError("down", ErrorKind.TRANSIENT))
    run_job(make_ctx(web, job, FakeLLM(), search))
    db.session.refresh(job)
    assert job.status in ("completed", "completed_with_warnings"), (job.status, job.error_message)
    srcs = db.session.scalars(select(ResearchSource).where(ResearchSource.job_id == job.id)).all()
    assert [s.origin for s in srcs] == ["upload"]
    assert "notes.txt" in section_bodies(job)["references"]


def test_crash_inside_a_node_fails_the_job_not_hangs_it(app, alice, web):
    def boom(request, llm):
        raise RuntimeError("secret tvly-ABCDEFGHIJKLMNOPQRSTUV leaked")

    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, FakeLLM(planner=boom)))
    db.session.refresh(job)
    assert job.status == "failed" and job.error_code == "internal_error"
    assert "tvly" not in (job.error_message or "") and not job.is_active


# ------------------------------------------------------------------ citation helpers
def test_citation_helpers():
    body, removed = citations.clean_citations("A [@S1] b [@S2][@S3] c", {1, 3})
    assert removed == [2] and "[@S2]" not in body and "[@S1]" in body and "[@S3]" in body
    assert citations.cited_ids("x [@S5] y [@S2] [@S5]") == [5, 2]
    texts = ["Panels convert about 22 percent of sunlight into electricity today."]
    assert (
        citations.unverified_quotes('He said "convert about 22 percent of sunlight into electricity".', texts)
        == []
    )
    assert citations.unverified_quotes(
        'He said "panels convert nearly all sunlight into power instantly".', texts
    )
    assert citations.unverified_quotes('The "short phrase" is fine.', texts) == []
    assert (
        citations.uncited_paragraphs("x" * 200) == 1
        and citations.uncited_paragraphs("x" * 200 + " [@S1]") == 0
    )


def test_subtopic_without_evidence_is_reported_not_papered_over(app, alice, web):
    only_one = FakeSearch([web.url("/page/1")])  # every query finds the same single page
    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, FakeLLM(), only_one))
    db.session.refresh(job)
    assert job.status == "completed_with_warnings"
    assert any(i["type"] == "uncovered_topic" for i in job.unresolved_issues)
    assert "does not cover" in section_bodies(job)["temperature"]
    assert "Temperature effects" in section_bodies(job)["limitations"]


def test_no_llm_keys_gives_a_configuration_message(app, alice, web):
    from providers.base import NoProviderConfiguredError

    class NoKeys:
        def generate(self, request):
            raise NoProviderConfiguredError("none", [])

    job = new_job(alice, depth="quick")
    run_job(make_ctx(web, job, NoKeys()))
    db.session.refresh(job)
    assert (
        job.status == "failed"
        and job.error_code == "llm_not_configured"
        and "GEMINI_API_KEY_1" in job.error_message
    )
