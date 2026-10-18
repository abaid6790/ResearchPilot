"""Background jobs: starting, claiming, cancelling, resuming, live progress and the report page."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import jobs as jobs_module
from agents.runner import run_job
from core.audit import AuditEvent
from core.db_utils import utcnow
from extensions import db
from reports.render import section_html
from research.models import ResearchJob
from research.services import ActiveJobExistsError, create_job, create_project, resume_job
from tests.conftest import get_csrf, login, make_user
from tests.fake_llm import FakeLLM
from tests.fake_web import FakeWeb
from tests.test_agents import make_ctx, report_of


@pytest.fixture()
def web():
    with FakeWeb() as w:
        yield w


@pytest.fixture()
def alice(app):
    return make_user("alice@example.com", name="Alice")


@pytest.fixture()
def bob(app):
    return make_user("bob@example.com", name="Bob")


@pytest.fixture()
def alice_client(client, alice):
    login(client, "alice@example.com")
    return client


@pytest.fixture()
def configured(app):
    """Pretend keys exist so the start button is allowed to proceed (nothing is ever called)."""
    app.config["LLM_ENVIRON"] = {"GEMINI_API_KEY_1": "AIza" + "x" * 35}
    app.config["TOOLS_ENVIRON"] = {"TAVILY_API_KEY": "tvly-" + "y" * 30}


def post(client, path, page=None):
    return client.post(path, data={"csrf_token": get_csrf(client, page or "/research/")})


def project_for(user, **settings):
    return create_project(
        user, title="Solar", topic="How efficient are solar panels today?", settings=settings
    )


# ------------------------------------------------------------------ start
def test_start_needs_login_and_ownership(client, alice, bob, configured):
    project = project_for(alice)
    assert client.post(f"/research/{project.public_id}/start").status_code in (302, 400, 401)
    login(client, "bob@example.com")
    assert post(client, f"/research/{project.public_id}/start").status_code == 404
    assert db.session.scalar(select(db.func.count()).select_from(ResearchJob)) == 0


def test_start_without_keys_explains_instead_of_failing_later(alice_client, alice):
    project = project_for(alice)
    page = f"/research/{project.public_id}"
    r = post(alice_client, f"{page}/start", page)
    assert r.status_code == 302
    html = " ".join(alice_client.get(page).get_data(as_text=True).split())
    assert "No AI provider key is configured" in html and "TAVILY_API_KEY" in html
    assert db.session.scalar(select(db.func.count()).select_from(ResearchJob)) == 0


def test_start_creates_a_queued_job_and_audit_event(alice_client, alice, configured):
    project = project_for(alice)
    page = f"/research/{project.public_id}"
    assert post(alice_client, f"{page}/start", page).status_code == 302
    job = db.session.scalars(select(ResearchJob)).one()
    assert job.status == "queued" and job.project_id == project.id
    assert db.session.scalar(select(AuditEvent).where(AuditEvent.event_type == "job_started")) is not None
    html = alice_client.get(page).get_data(as_text=True)
    assert "data-job-panel" in html and "Research job queued." in html


def test_second_start_is_refused_while_a_run_is_active(alice_client, alice, configured):
    project = project_for(alice)
    page = f"/research/{project.public_id}"
    post(alice_client, f"{page}/start", page)
    post(alice_client, f"{page}/start", page)
    assert db.session.scalar(select(db.func.count()).select_from(ResearchJob)) == 1


def test_per_user_cap_on_simultaneous_runs(alice_client, alice, app, configured):
    app.config["MAX_ACTIVE_JOBS_PER_USER"] = 1
    first, second = project_for(alice), project_for(alice)
    post(alice_client, f"/research/{first.public_id}/start", f"/research/{first.public_id}")
    post(alice_client, f"/research/{second.public_id}/start", f"/research/{second.public_id}")
    assert db.session.scalar(select(db.func.count()).select_from(ResearchJob)) == 1


def test_archived_projects_cannot_start(alice_client, alice, configured):
    project = project_for(alice)
    project.archived_at = utcnow()
    db.session.commit()
    post(alice_client, f"/research/{project.public_id}/start", "/research/")
    assert db.session.scalar(select(db.func.count()).select_from(ResearchJob)) == 0


# ------------------------------------------------------------------ claiming and running
def test_claim_is_exclusive_and_stale_claims_expire(app, alice):
    job = create_job(project_for(alice))
    assert jobs_module.claim(job.id, 300) is True
    assert jobs_module.claim(job.id, 300) is False  # someone is running it
    job.heartbeat_at = utcnow() - timedelta(seconds=1000)
    db.session.commit()
    assert job in jobs_module.stale_active_jobs(app)
    assert jobs_module.claim(job.id, 300) is True  # its worker died: taken over


def test_finished_or_unknown_jobs_are_not_claimed(app, alice):
    job = create_job(project_for(alice))
    job.status = "completed"
    db.session.commit()
    assert jobs_module.claim(job.id, 300) is False
    assert jobs_module.run_by_public_id(app, "does-not-exist") is False


def test_run_by_public_id_runs_the_workflow(app, alice, web, monkeypatch):
    job = create_job(project_for(alice, depth="quick"))
    monkeypatch.setattr("agents.runner.default_context", lambda a, jid: make_ctx(web, job, FakeLLM()))
    assert jobs_module.run_by_public_id(app, job.public_id) is True
    db.session.refresh(job)
    assert job.status == "completed" and job.heartbeat_at is not None


def test_a_job_claimed_elsewhere_is_not_run_twice(app, alice, web, monkeypatch):
    job = create_job(project_for(alice, depth="quick"))
    jobs_module.claim(job.id, 300)
    llm = FakeLLM()
    monkeypatch.setattr("agents.runner.default_context", lambda a, jid: make_ctx(web, job, llm))
    assert jobs_module.run_by_public_id(app, job.public_id) is False
    assert llm.calls == []


def test_thread_runner_submits_and_manual_runner_does_not(app, alice, monkeypatch):
    job = create_job(project_for(alice))
    submitted = []

    class Pool:
        def submit(self, fn, *args):
            submitted.append(args[1])

    monkeypatch.setattr(jobs_module, "_pool", lambda a: Pool())
    app.config["JOB_RUNNER"] = "thread"
    assert jobs_module.enqueue(app, job) == "thread" and submitted == [job.public_id]
    app.config["JOB_RUNNER"] = "manual"
    assert jobs_module.enqueue(app, job) == "manual" and len(submitted) == 1
    app.config["JOB_RUNNER"] = "bogus"
    with pytest.raises(ValueError):
        jobs_module.enqueue(app, job)


def test_celery_enqueue_sends_the_task_and_worker_task_runs_the_job(app, alice, web, monkeypatch):
    from celery import Celery

    from jobs.celery_app import TASK_NAME, make_worker_app

    app.config["JOB_RUNNER"], app.config["CELERY_BROKER_URL"] = "celery", "redis://localhost:6379/9"
    sent = []

    class Result:
        id = "task-123"

    monkeypatch.setattr(
        Celery, "send_task", lambda self, name, args=None, **kw: (sent.append((name, args)), Result())[1]
    )
    job = create_job(project_for(alice, depth="quick"))
    assert jobs_module.enqueue(app, job) == "celery"
    assert sent == [(TASK_NAME, [job.public_id])]
    db.session.refresh(job)
    assert job.celery_task_id == "task-123"

    monkeypatch.setattr("agents.runner.default_context", lambda a, jid: make_ctx(web, job, FakeLLM()))
    worker = make_worker_app(app)
    assert worker.tasks[TASK_NAME].apply(args=[job.public_id]).get() is True
    db.session.refresh(job)
    assert job.status == "completed"


def test_celery_needs_a_broker(app, alice):
    from jobs.celery_app import send

    app.config["CELERY_BROKER_URL"] = ""
    with pytest.raises(RuntimeError, match="CELERY_BROKER_URL"):
        send(app, "x")


# ------------------------------------------------------------------ cancel
def test_cancelling_a_queued_job_works_without_a_worker(alice_client, alice):
    project = project_for(alice)
    job = create_job(project)
    page = f"/research/{project.public_id}"
    post(alice_client, f"{page}/jobs/{job.public_id}/cancel", page)
    db.session.refresh(job)
    assert job.status == "cancelled" and job.finished_at


def test_cancelling_a_running_job_stops_it(alice_client, alice, web):
    project = project_for(alice, depth="quick")
    job = create_job(project)
    jobs_module.claim(job.id, 300)  # a worker has it
    page = f"/research/{project.public_id}"
    post(alice_client, f"{page}/jobs/{job.public_id}/cancel", page)
    db.session.refresh(job)
    assert job.status == "queued" and job.cancel_requested_at is not None  # waits for the worker to notice
    llm = FakeLLM()
    run_job(make_ctx(web, job, llm))
    db.session.refresh(job)
    assert job.status == "cancelled" and llm.calls == []


def test_cancel_is_owner_only_and_finished_jobs_say_so(alice_client, alice, bob, client):
    project = project_for(alice)
    job = create_job(project)
    job.status = "completed"
    db.session.commit()
    page = f"/research/{project.public_id}"
    r = post(alice_client, f"{page}/jobs/{job.public_id}/cancel", page)
    assert r.status_code == 302
    assert "already finished" in " ".join(alice_client.get(page).get_data(as_text=True).split())
    other = project_for(bob)
    assert (
        post(alice_client, f"/research/{other.public_id}/jobs/{job.public_id}/cancel", page).status_code
        == 404
    )
    assert post(alice_client, f"{page}/jobs/{'0' * 32}/cancel", page).status_code == 404


# ------------------------------------------------------------------ resume
def failed_job(user, code="llm_unavailable"):
    job = create_job(project_for(user))
    job.status, job.error_code, job.error_message, job.finished_at = (
        "failed",
        code,
        "Provider down.",
        utcnow(),
    )
    db.session.commit()
    return job


def test_resume_requeues_a_resumable_failure(alice_client, alice, configured):
    job = failed_job(alice)
    page = f"/research/{job.project.public_id}"
    html = alice_client.get(page).get_data(as_text=True)
    assert "Resume from saved progress" in html and "Provider down." in html
    post(alice_client, f"{page}/jobs/{job.public_id}/resume", page)
    db.session.refresh(job)
    assert job.status == "queued" and job.error_code is None and job.finished_at is None
    assert "job_resumed" in [e.event_type for e in job.events]


def test_resume_refuses_non_resumable_and_conflicting_jobs(app, alice):
    cancelled = failed_job(alice, code="cancelled")
    assert resume_job(cancelled, alice.id) is False
    job = failed_job(alice)
    create_job(job.project)  # another active job on the same project
    with pytest.raises(ActiveJobExistsError):
        resume_job(job, alice.id)
    db.session.refresh(job)
    assert job.status == "failed"


# ------------------------------------------------------------------ progress endpoints
def test_events_endpoint_returns_only_new_events(alice_client, alice):
    job = create_job(project_for(alice))
    base = f"/research/{job.project.public_id}/jobs/{job.public_id}"
    data = alice_client.get(f"{base}/events").get_json()
    assert data["status"] == "queued" and data["finished"] is False
    assert [e["type"] for e in data["events"]] == ["job_queued"]
    assert alice_client.get(f"{base}/events?after={data['last_seq']}").get_json()["events"] == []


def test_events_endpoint_is_owner_only(app, alice, bob):
    job = create_job(project_for(alice))
    other = app.test_client()
    login(other, "bob@example.com")
    assert other.get(f"/research/{job.project.public_id}/jobs/{job.public_id}/events").status_code == 404
    assert other.get(f"/research/{job.project.public_id}/jobs/{job.public_id}/stream").status_code == 404


def test_stream_sends_events_then_done_and_honours_last_event_id(alice_client, alice):
    job = create_job(project_for(alice))
    job.status = "completed"
    job.finished_at = utcnow()
    db.session.commit()
    url = f"/research/{job.project.public_id}/jobs/{job.public_id}/stream"
    r = alice_client.get(url)
    body = r.get_data(as_text=True)
    assert r.mimetype == "text/event-stream" and r.headers["Cache-Control"].startswith("no-cache")
    assert "event: progress" in body and "Research job queued." in body and "event: done" in body
    resumed = alice_client.get(url, headers={"Last-Event-ID": "1"}).get_data(as_text=True)
    assert "event: progress" not in resumed and "event: done" in resumed


def test_stream_for_an_active_job_ends_by_itself_so_workers_are_not_held(alice_client, alice, app):
    app.config["JOB_SSE_MAX_SECONDS"] = 1
    job = create_job(project_for(alice))
    body = alice_client.get(f"/research/{job.project.public_id}/jobs/{job.public_id}/stream").get_data(
        as_text=True
    )
    assert "retry: 3000" in body and "event: done" not in body and ": reconnect" in body


def test_progress_script_and_markup_are_csp_safe(alice_client, alice):
    job = create_job(project_for(alice))
    r = alice_client.get(f"/research/{job.project.public_id}")
    html = r.get_data(as_text=True)
    assert (
        "progress.js" in html and "<script>" not in html and " onclick=" not in html and "style=" not in html
    )
    js = alice_client.get("/static/js/research/progress.js").get_data(as_text=True)
    assert "innerHTML" not in js and "eval(" not in js


# ------------------------------------------------------------------ report page
def run_to_report(user, web, **settings):
    job = create_job(project_for(user, depth="quick", **settings))
    run_job(make_ctx(web, job, FakeLLM()))
    db.session.refresh(job)
    return job


def test_report_page_shows_numbered_citations_and_references(alice_client, alice, web):
    job = run_to_report(alice, web)
    html = alice_client.get(f"/research/{job.project.public_id}/report").get_data(as_text=True)
    assert 'class="cite"' in html and 'href="#ref-1"' in html and 'id="ref-1"' in html
    assert "[@S" not in html  # markers are never shown raw
    assert "http://site.test:" in html and "read in full" in html
    assert "No major problems were found" in html and "does not prove" in html
    assert "Limitations and uncertainty" in html


def test_report_page_warns_when_checks_found_problems(alice_client, alice, web):
    job = create_job(project_for(alice, depth="quick"))
    llm = FakeLLM()
    llm.fail_tasks = {"validator"}
    run_job(make_ctx(web, job, llm))
    html = alice_client.get(f"/research/{job.project.public_id}/report").get_data(as_text=True)
    assert "Not fully verified" in html and "could not be checked" in html


def test_report_page_is_owner_only_and_handles_no_report(alice_client, alice, bob, app):
    project = project_for(alice)
    assert alice_client.get(f"/research/{project.public_id}/report").status_code == 302
    other = app.test_client()
    login(other, "bob@example.com")
    assert other.get(f"/research/{project.public_id}/report").status_code == 404


def test_report_html_is_sanitised_and_citations_are_ours_alone():
    html = section_html(
        "Fine text [@S1] and <script>alert(1)</script><img src=x onerror=alert(1)> "
        '[click](javascript:alert(1)) <a href="https://evil.example">link</a> [@S99]\n\n- item',
        {1: 1},
    )
    assert (
        "<script" not in html and "onerror" not in html and "javascript:" not in html and "<img" not in html
    )
    assert "evil.example" not in html
    assert '<sup class="cite"><a href="#ref-1"' in html and "S99" not in html and "<li>item</li>" in html


def test_report_titles_from_the_web_are_escaped(alice_client, alice, web):
    from research.models import ResearchSource

    job = run_to_report(alice, web)
    src = db.session.scalars(select(ResearchSource).where(ResearchSource.job_id == job.id)).first()
    src.title = "<img src=x onerror=alert(1)>"
    db.session.commit()
    html = alice_client.get(f"/research/{job.project.public_id}/report").get_data(as_text=True)
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert report_of(job)  # report still exists


def test_recover_command_lists_and_requeues_stale_jobs(app, alice):
    job = create_job(project_for(alice))
    job.heartbeat_at = utcnow() - timedelta(hours=1)
    db.session.commit()
    result = app.test_cli_runner().invoke(args=["recover-jobs"])
    assert job.public_id in result.output and "1 job(s) re-queued" in result.output
