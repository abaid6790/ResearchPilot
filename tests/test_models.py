"""Phase 4 data model: constraints, ownership isolation, deletion rules, services, audit, usage."""

from __future__ import annotations

import hashlib
import threading

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import NotFound

from auth.models import User
from core.audit import AuditEvent, record_event
from extensions import db
from providers.base import UsageRecord
from providers.usage import ProviderUsage, make_db_recorder
from reports.models import QualityEvaluation, ReportDocument, ReportRevision, ReportSection
from research import access
from research.models import (
    ClaimSource,
    EvidenceExcerpt,
    ResearchClaim,
    ResearchJob,
    ResearchJobEvent,
    ResearchProject,
    ResearchSource,
    ResearchTask,
)
from research.schemas import ResearchSettings
from research.services import (
    ActiveJobExistsError,
    append_event,
    create_job,
    create_project,
    events_after,
    request_cancel,
)
from tests.conftest import make_user


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def count(model) -> int:
    return db.session.scalar(select(func.count()).select_from(model))


def build_graph(user, name="a"):
    """A complete research graph for one user: every table gets at least one row."""
    project = create_project(user, title=f"Project {name}", topic="How do heat pumps work?")
    job = create_job(project)
    task = ResearchTask(job_id=job.id, kind="question", agent="planner", title="What is COP?")
    db.session.add(task)
    db.session.flush()
    source = ResearchSource(
        job_id=job.id, task_id=task.id, url=f"https://example.com/{name}", canonical_url=f"https://example.com/{name}",
        url_hash=digest(f"https://example.com/{name}"), domain="example.com", title="Heat pumps",
    )  # fmt: skip
    db.session.add(source)
    db.session.flush()
    excerpt = EvidenceExcerpt(
        source_id=source.id, task_id=task.id, text="COP is a ratio.", content_hash=digest("x")
    )
    claim = ResearchClaim(job_id=job.id, task_id=task.id, text="COP measures efficiency.")
    db.session.add_all([excerpt, claim])
    db.session.flush()
    link = ClaimSource(claim_id=claim.id, source_id=source.id, job_id=job.id, excerpt_id=excerpt.id)
    report = ReportDocument(project_id=project.id, job_id=job.id, title="Report")
    db.session.add_all([link, report])
    db.session.flush()
    section = ReportSection(
        report_id=report.id, key="summary", heading="Summary", body=f"COP [@S{source.id}]"
    )
    db.session.add(section)
    db.session.flush()
    revision = ReportRevision(
        report_id=report.id, revision_no=1, kind="section", section_id=section.id, section_key="summary",
        source="agent_writer", content=section.body,
    )  # fmt: skip
    evaluation = QualityEvaluation(
        report_id=report.id, run_id="r" * 32, evaluator="deterministic", dimension="citation_integrity",
        score=90, rubric_version="1", method="share of citations resolving to a source",
    )  # fmt: skip
    db.session.add_all([revision, evaluation])
    db.session.commit()
    return {"project": project, "job": job, "task": task, "source": source, "excerpt": excerpt, "claim": claim,
            "link": link, "report": report, "section": section, "revision": revision, "evaluation": evaluation}  # fmt: skip


@pytest.fixture()
def alice(app):
    return make_user("alice@example.com", name="Alice")


@pytest.fixture()
def bob(app):
    return make_user("bob@example.com", name="Bob")


# ----------------------------------------------------------------------- ownership
def test_user_cannot_reach_another_users_rows_by_public_or_internal_id(alice, bob):
    a = build_graph(alice, "a")
    lookups = [
        (access.owned_project, a["project"].public_id), (access.owned_job, a["job"].public_id),
        (access.owned_report, a["report"].public_id), (access.owned_task, a["task"].id),
        (access.owned_source, a["source"].id), (access.owned_excerpt, a["excerpt"].id),
        (access.owned_claim, a["claim"].id), (access.owned_claim_link, a["link"].id),
        (access.owned_section, a["section"].id), (access.owned_revision, a["revision"].id),
        (access.owned_evaluation, a["evaluation"].id),
    ]  # fmt: skip
    for loader, key in lookups:
        assert loader(alice, key) is not None  # the owner can load it
        with pytest.raises(NotFound):  # anyone else gets 404, exactly as if it did not exist
            loader(bob, key)


def test_missing_and_foreign_rows_look_identical(alice, bob):
    a = build_graph(alice)
    with pytest.raises(NotFound) as foreign:
        access.owned_project(bob, a["project"].public_id)
    with pytest.raises(NotFound) as missing:
        access.owned_project(bob, "0" * 32)
    assert foreign.value.description == missing.value.description


def test_project_list_only_contains_own_projects(alice, bob):
    build_graph(alice, "a")
    build_graph(bob, "b")
    mine = db.session.scalars(access.projects_of(alice)).all()
    assert [p.title for p in mine] == ["Project a"]


def test_public_ids_are_random_and_unique(alice):
    ids = {create_project(alice, title=f"p{i}", topic="t").public_id for i in range(5)}
    assert len(ids) == 5 and all(len(i) == 32 for i in ids)


# ----------------------------------------------------------------- deletion rules
def test_deleting_a_user_removes_their_research_but_keeps_usage_and_audit_trail(alice, bob):
    a = build_graph(alice, "a")
    build_graph(bob, "b")
    db.session.add(ProviderUsage(user_id=alice.id, job_id=a["job"].id, request_id="r1", task="planner",
                                 provider="gemini", outcome="ok", latency_ms=10))  # fmt: skip
    db.session.commit()

    db.session.delete(db.session.get(User, alice.id))
    db.session.commit()

    for model in (ResearchProject, ResearchJob, ResearchTask, ResearchSource, EvidenceExcerpt, ResearchClaim,
                  ClaimSource, ReportDocument, ReportSection, ReportRevision, QualityEvaluation):  # fmt: skip
        assert count(model) == 1, model.__name__  # only Bob's single graph remains
    usage = db.session.scalars(select(ProviderUsage)).one()
    assert usage.user_id is None and usage.job_id is None  # totals survive, link to the person does not
    assert db.session.scalars(select(AuditEvent).where(AuditEvent.event_type == "project_created")).all()
    assert all(e.actor_user_id != alice.id for e in db.session.scalars(select(AuditEvent)))


def test_deleting_a_project_cascades_through_everything_below_it(alice):
    a = build_graph(alice)
    db.session.delete(a["project"])
    db.session.commit()
    for model in (ResearchJob, ResearchJobEvent, ResearchTask, ResearchSource, EvidenceExcerpt, ResearchClaim,
                  ClaimSource, ReportDocument, ReportSection, ReportRevision, QualityEvaluation):  # fmt: skip
        assert count(model) == 0, model.__name__


def test_deleting_a_section_keeps_its_revision_history(alice):
    a = build_graph(alice)
    db.session.delete(a["section"])
    db.session.commit()
    revision = db.session.scalars(select(ReportRevision)).one()
    assert revision.section_id is None and revision.section_key == "summary"


def test_deleting_a_source_removes_its_excerpts_and_claim_links_but_not_the_claim(alice):
    a = build_graph(alice)
    db.session.delete(a["source"])
    db.session.commit()
    assert count(EvidenceExcerpt) == 0 and count(ClaimSource) == 0 and count(ResearchClaim) == 1


# ---------------------------------------------------------- database-level constraints
def violates(*objects) -> None:
    db.session.add_all(objects)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_job_status_vocabulary_is_enforced(alice):
    project = create_project(alice, title="t", topic="t")
    violates(ResearchJob(project_id=project.id, status="running"))  # not a defined status


def test_only_one_active_job_per_project(alice):
    project = create_project(alice, title="t", topic="t")
    first = create_job(project)
    with pytest.raises(ActiveJobExistsError):
        create_job(project)
    first.status = "completed"
    db.session.commit()
    assert create_job(project).id != first.id  # allowed again once the first one finished


def test_concurrent_job_creation_allows_exactly_one(app, alice):
    project = create_project(alice, title="t", topic="t")
    results = []

    def attempt():
        with app.app_context():
            try:
                results.append(create_job(db.session.get(ResearchProject, project.id)))
            except ActiveJobExistsError:
                results.append(None)
            finally:
                db.session.remove()

    if db.engine.dialect.name == "sqlite":
        pytest.skip("in-memory SQLite shares one connection; the race is only meaningful on PostgreSQL")
    threads = [threading.Thread(target=attempt) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    assert sum(r is not None for r in results) == 1


def test_source_needs_a_locator_and_unique_url_per_job(alice):
    a = build_graph(alice)
    violates(ResearchSource(job_id=a["job"].id, title="no url and no upload"))
    violates(ResearchSource(job_id=a["job"].id, url="https://example.com/a", url_hash=a["source"].url_hash))


def test_score_and_vocabulary_ranges_are_enforced(alice):
    a = build_graph(alice)
    j = a["job"].id
    violates(ResearchSource(job_id=j, url="https://x.test/1", url_hash="1" * 64, relevance_score=1.5))
    violates(ResearchSource(job_id=j, url="https://x.test/2", url_hash="2" * 64, link_status="verified"))
    violates(ResearchClaim(job_id=j, text="c", support_status="true"))
    violates(ResearchClaim(job_id=j, text="c", confidence=2))
    rid = a["report"].id
    violates(QualityEvaluation(report_id=rid, run_id="s" * 32, evaluator="deterministic", dimension="vibes",
                               score=50, rubric_version="1", method="m"))  # fmt: skip
    violates(QualityEvaluation(report_id=rid, run_id="s" * 32, evaluator="llm", dimension="clarity_structure",
                               score=101, rubric_version="1", method="m"))  # fmt: skip
    violates(QualityEvaluation(report_id=rid, run_id="s" * 32, evaluator="llm", dimension="completeness",
                               status="scored", score=None, rubric_version="1", method="m"))  # fmt: skip
    violates(QualityEvaluation(report_id=rid, run_id="s" * 32, evaluator="llm", dimension="completeness",
                               status="not_assessed", score=40, rubric_version="1", method="m"))  # fmt: skip


def test_duplicate_evaluation_dimension_in_one_run_is_rejected(alice):
    a = build_graph(alice)
    dup = a["evaluation"]
    violates(QualityEvaluation(report_id=dup.report_id, run_id=dup.run_id, evaluator=dup.evaluator,
                               dimension=dup.dimension, score=10, rubric_version="1", method="m"))  # fmt: skip


def test_revision_numbers_are_unique_per_report_and_sections_need_a_key(alice):
    a = build_graph(alice)
    rid = a["report"].id
    violates(ReportRevision(report_id=rid, revision_no=1, kind="document", source="user_edit"))
    violates(
        ReportRevision(report_id=rid, revision_no=9, kind="section", source="user_edit")
    )  # no section_key
    violates(
        ReportRevision(report_id=rid, revision_no=9, kind="document", source="user_edit", status="maybe")
    )


def test_claim_cannot_cite_a_source_from_another_job(alice, bob):
    a, b = build_graph(alice, "a"), build_graph(bob, "b")
    # A claim of Alice's job pointing at a source of Bob's job violates the composite foreign key.
    violates(
        ClaimSource(claim_id=a["claim"].id, source_id=b["source"].id, job_id=a["job"].id, relation="context")
    )
    violates(
        ClaimSource(claim_id=a["claim"].id, source_id=a["source"].id, job_id=b["job"].id, relation="context")
    )


def test_claim_source_relation_is_unique_and_validated(alice):
    a = build_graph(alice)
    link = a["link"]
    violates(
        ClaimSource(claim_id=link.claim_id, source_id=link.source_id, job_id=link.job_id, relation="supports")
    )
    violates(
        ClaimSource(claim_id=link.claim_id, source_id=link.source_id, job_id=link.job_id, relation="proves")
    )


def test_report_per_job_is_unique(alice):
    a = build_graph(alice)
    violates(ReportDocument(project_id=a["project"].id, job_id=a["job"].id, title="second"))


def test_blank_project_title_is_rejected(alice):
    violates(ResearchProject(user_id=alice.id, title="   ", topic="t"))


def test_relationships_load_the_graph(alice):
    a = build_graph(alice)
    db.session.expire_all()
    job = db.session.get(ResearchJob, a["job"].id)
    assert [t.title for t in job.tasks] == ["What is COP?"]
    claim = db.session.get(ResearchClaim, a["claim"].id)
    assert claim.links[0].source.title == "Heat pumps"
    report = db.session.get(ReportDocument, a["report"].id)
    assert report.sections[0].heading == "Summary" and report.revisions[0].revision_no == 1


# ------------------------------------------------------------------ settings schema
def test_settings_defaults_follow_the_depth_preset():
    s = ResearchSettings.model_validate({"depth": "deep"})
    assert (s.max_sources, s.max_research_rounds, s.max_revision_rounds) == (30, 3, 2)
    assert (
        ResearchSettings.model_validate({"depth": "quick", "max_revision_rounds": 0}).max_revision_rounds == 0
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"depth": "extreme"},
        {"max_sources": 1000},
        {"max_research_rounds": 99},
        {"max_revision_rounds": -1},
        {"unknown_field": 1},
        {"exclude_domains": ["not a domain"]},
        {"prioritize_domains": ["a.com"], "exclude_domains": ["https://www.a.com/x"]},
        {"date_from": "2026-13-45"},
        {"date_from": "2026-05-01", "date_to": "2026-01-01"},
        {"source_categories": ["astrology"]},
        {"language": "english"},
    ],
)
def test_invalid_settings_are_rejected(bad):
    with pytest.raises(ValidationError):
        ResearchSettings.model_validate(bad)


def test_domains_are_normalised_and_deduplicated():
    s = ResearchSettings.model_validate({"exclude_domains": ["HTTPS://WWW.Example.com/path", "example.com"]})
    assert s.exclude_domains == ["example.com"]


def test_create_project_validates_and_stores_settings(alice):
    project = create_project(alice, title="  T  ", topic=" topic ", settings={"depth": "deep"})
    assert (project.title, project.topic, project.depth) == ("T", "topic", "deep")
    assert project.settings["max_sources"] == 30
    with pytest.raises(ValidationError):
        create_project(alice, title="x", topic="y", settings={"depth": "nope"})
    assert count(ResearchProject) == 1  # the invalid one was not saved


# -------------------------------------------------------------- jobs, events, cancel
def test_event_sequence_is_gapless_and_resumable(alice):
    job = create_job(create_project(alice, title="t", topic="t"))
    for i in range(4):
        append_event(job, "query_run", f"query {i}", stage="researching", agent="researcher", data={"n": i})
    db.session.commit()
    events = events_after(job, 0)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]  # 1 is "job_queued"
    assert [e.message for e in events_after(job, 3)] == ["query 2", "query 3"]  # reconnect after seq 3
    assert events_after(job, 5) == []


def test_events_never_store_secrets(alice):
    job = create_job(create_project(alice, title="t", topic="t"))
    key = "AIzaSyFakeGeminiKeyNumber1ForTestsOnly000"
    event = append_event(job, "note", f"failed with key={key}", data={"detail": f"Bearer {key}"})
    db.session.commit()
    assert key not in event.message and key not in str(event.data)


def test_cancel_marks_active_jobs_and_ignores_finished_ones(alice):
    job = create_job(create_project(alice, title="t", topic="t"))
    assert request_cancel(job, alice.id) is True
    assert job.cancel_requested_at is not None and events_after(job, 1)[-1].event_type == "cancel_requested"
    job.status = "completed"
    db.session.commit()
    assert request_cancel(job, alice.id) is False


def test_job_snapshot_is_independent_of_later_project_edits(alice):
    project = create_project(alice, title="t", topic="t", settings={"depth": "quick"})
    job = create_job(project)
    project.settings = {**project.settings, "max_sources": 99}
    db.session.commit()
    assert job.config_snapshot["max_sources"] == 8


# ------------------------------------------------------------------------- audit
def test_audit_events_are_recorded_for_account_actions(client, app):
    from tests.conftest import PASSWORD, login, post_form

    post_form(client, "/auth/register", {"display_name": "Ada", "email": "ada2@example.com",
                                         "password": PASSWORD, "confirm_password": PASSWORD}, csrf_from="/auth/register")  # fmt: skip
    user = db.session.scalars(select(User).where(User.email == "ada2@example.com")).one()
    user.email_verified_at = None
    db.session.commit()
    types = [e.event_type for e in db.session.scalars(select(AuditEvent).order_by(AuditEvent.id))]
    assert types == ["user_registered"]
    make_user("ada@example.com")
    login(client)
    assert "login_succeeded" in [e.event_type for e in db.session.scalars(select(AuditEvent))]


def test_audit_rejects_unknown_types_and_sensitive_details(alice):
    with pytest.raises(ValueError):
        record_event("something_else")
    for key in ("password", "reset_token", "api_key", "email"):
        with pytest.raises(ValueError):
            record_event("login_succeeded", **{key: "x"})


def test_audit_event_is_discarded_with_a_rolled_back_action(alice):
    record_event("project_created", actor_user_id=alice.id)
    db.session.rollback()
    assert count(AuditEvent) == 0


def test_audit_details_are_redacted(alice):
    record_event(
        "report_exported", actor_user_id=alice.id, note="key AIzaSyFakeGeminiKeyNumber1ForTestsOnly000"
    )
    db.session.commit()
    assert "AIzaSy" not in str(db.session.scalars(select(AuditEvent)).one().details)


# --------------------------------------------------------------------- usage records
def test_usage_recorder_persists_counts_without_content(app, alice):
    record = make_db_recorder(app)
    record(UsageRecord(request_id="abc", task="planner", provider="gemini", model="m", key_label="gemini#2",
                       outcome="ok", latency_s=1.234, prompt_tokens=10, completion_tokens=5, http_status=200,
                       user_id=alice.id))  # fmt: skip
    row = db.session.scalars(select(ProviderUsage)).one()
    assert (row.provider, row.key_label, row.latency_ms, row.prompt_tokens, row.user_id) == (
        "gemini",
        "gemini#2",
        1234,
        10,
        alice.id,
    )
    columns = {c.name for c in ProviderUsage.__table__.columns}
    assert not columns & {"prompt", "response", "content", "api_key", "secret"}


def test_usage_recorder_failure_is_swallowed(app, caplog):
    record = make_db_recorder(app)
    record(
        UsageRecord(
            request_id="x",
            task="t",
            provider="p",
            model=None,
            key_label=None,
            outcome="ok",
            latency_s=0.1,
            user_id=999999,
        )
    )  # nonexistent user: FK violation on PostgreSQL
    # Must not raise either way; on SQLite foreign keys are enforced too (pragma enabled).
    assert count(ProviderUsage) == 0


def test_router_records_user_and_job_for_each_call(app, alice):
    from providers import build_router
    from providers.base import LLMRequest
    from tests.fake_providers import FakeProviders
    from tests.test_providers import G

    fake = FakeProviders().start()
    try:
        router = build_router(fake.env(GEMINI_API_KEY_1=G[0]), usage_recorder=make_db_recorder(app))
        router.generate(LLMRequest.simple("hi", user_id=alice.id, task="planner"))
    finally:
        fake.stop()
    row = db.session.scalars(select(ProviderUsage)).one()
    assert (row.user_id, row.task, row.outcome, row.key_label) == (alice.id, "planner", "ok", "gemini#1")
