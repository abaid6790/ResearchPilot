"""Phase 8: sidebar shell, saved reports, history, dashboard activity/usage, run summary."""

from __future__ import annotations

import pytest

from extensions import db
from providers.usage import ProviderUsage
from reports.models import ReportDocument
from research.services import append_event, create_job, create_project, job_summary
from tests.conftest import login, make_user


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


def make_report(user, title, *, ready=True, status="final"):
    project = create_project(user, title=f"Proj {title}", topic="How do heat pumps work?")
    job = create_job(project)
    job.status = "completed"
    report = ReportDocument(
        project_id=project.id, job_id=job.id, title=title, status="draft", export_ready=ready
    )
    db.session.add(report)
    db.session.commit()
    return project, job, report


def test_sidebar_for_verified_users_only(alice_client, client):
    html = alice_client.get("/dashboard").get_data(as_text=True)
    assert 'id="sidebar"' in html and "data-sidebar-toggle" in html
    for path in ("/research/reports", "/research/history", "/research/new"):
        assert path in html
    assert "<script>" not in html and " onclick=" not in html


def test_auth_pages_have_no_sidebar(app):
    html = app.test_client().get("/auth/login").get_data(as_text=True)
    assert 'id="sidebar"' not in html


def test_pages_require_login(client):
    for path in ("/research/reports", "/research/history"):
        assert client.get(path).status_code in (302, 401)


def test_reports_lists_only_own_and_searches(alice_client, alice, bob):
    make_report(alice, "Heat pump efficiency")
    make_report(alice, "Solar panels")
    make_report(bob, "Bob secret report")
    html = alice_client.get("/research/reports").get_data(as_text=True)
    assert "Heat pump efficiency" in html and "Solar panels" in html
    assert "Bob secret report" not in html
    found = alice_client.get("/research/reports?q=solar").get_data(as_text=True)
    assert "Solar panels" in found and "Heat pump efficiency" not in found
    none = alice_client.get("/research/reports?q=zzzz").get_data(as_text=True)
    assert "No reports match" in none


def test_reports_empty_state_and_wildcards_are_literal(alice_client, alice):
    assert "No reports yet" in alice_client.get("/research/reports").get_data(as_text=True)
    make_report(alice, "Plain title")
    assert "Plain title" not in alice_client.get("/research/reports?q=%25").get_data(as_text=True)


def test_reports_pagination(alice_client, alice):
    for i in range(14):
        make_report(alice, f"Report number {i:02d}")
    page1 = alice_client.get("/research/reports").get_data(as_text=True)
    assert "Page 1 of" in page1
    assert alice_client.get("/research/reports?page=2").status_code == 200
    assert alice_client.get("/research/reports?page=abc").status_code == 200


def test_history_filters_and_ownership(alice_client, alice, bob):
    make_report(alice, "Done one")
    p, job, _ = make_report(alice, "Failed one")
    job.status = "failed"
    job.error_message = "No search key"
    db.session.commit()
    make_report(bob, "Bobs run")
    html = alice_client.get("/research/history").get_data(as_text=True)
    assert "Proj Done one" in html and "Proj Failed one" in html and "Bobs run" not in html
    failed = alice_client.get("/research/history?status=failed").get_data(as_text=True)
    assert "Proj Failed one" in failed and "Proj Done one" not in failed
    assert alice_client.get("/research/history?status=bogus").status_code == 200


def test_dashboard_activity_and_usage_are_real_and_private(alice_client, alice, bob):
    empty = alice_client.get("/dashboard").get_data(as_text=True)
    assert "Nothing has run yet" in empty and "No provider calls recorded" in empty
    assert "still being built" not in empty

    project = create_project(alice, title="Mine", topic="How do heat pumps work?")
    job = create_job(project)
    other = create_project(bob, title="Bobs", topic="How do heat pumps work?")
    create_job(other)
    db.session.add_all([
        ProviderUsage(user_id=alice.id, job_id=job.id, request_id="a1", task="planner", provider="gemini",
                      outcome="ok", latency_ms=5, prompt_tokens=10, completion_tokens=5),
        ProviderUsage(user_id=alice.id, job_id=job.id, request_id="a2", task="planner", provider="gemini",
                      outcome="rate_limit", latency_ms=5),
        ProviderUsage(user_id=bob.id, request_id="b1", task="planner", provider="groq", outcome="ok", latency_ms=5),
    ])  # fmt: skip
    db.session.commit()
    html = alice_client.get("/dashboard").get_data(as_text=True)
    assert "Mine" in html and "Research job queued." in html
    assert "Bobs" not in html
    assert "Gemini" in html and "Groq" not in html
    assert ">15<" in html  # only reported tokens are summed


def test_job_summary_counts_and_payload(alice_client, alice):
    project = create_project(alice, title="S", topic="How do heat pumps work?")
    job = create_job(project)
    append_event(job, "query_run", "Searched", stage="research")
    append_event(job, "query_run", "Searched again", stage="research")
    append_event(job, "source_failed", "Could not read", level="warning")
    db.session.commit()
    s = job_summary(job)
    assert s["queries"] == 2 and s["problems"] == 1 and s["tasks"] == []
    data = alice_client.get(f"/research/{project.public_id}/jobs/{job.public_id}/events").get_json()
    assert data["summary"]["queries"] == 2


def test_detail_page_shows_run_facts_without_inline_script(alice_client, alice):
    project = create_project(alice, title="S", topic="How do heat pumps work?")
    create_job(project)
    db.session.commit()
    html = alice_client.get(f"/research/{project.public_id}").get_data(as_text=True)
    assert "data-run-facts" in html and "data-run-tasks" in html
    assert "<script>" not in html
