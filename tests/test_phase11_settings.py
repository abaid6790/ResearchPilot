"""Phase 11: preferences, report defaults, notifications, data export, account deletion."""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from auth.models import User
from core.audit import AuditEvent
from core.db_utils import utcnow
from extensions import db
from providers.usage import ProviderUsage
from reports.models import ReportDocument
from research.models import ResearchJob, ResearchProject, ResearchSource
from settings import services as account
from tests.conftest import PASSWORD, login, make_user, post_form
from tests.test_phase9_editor import build
from tests.test_phase10_quality_exports import finish


@pytest.fixture()
def alice(app):
    return make_user("alice@example.com", name="Alice")


@pytest.fixture()
def bob(app):
    return make_user("bob@example.com", name="Bob")


@pytest.fixture()
def ac(client, alice):
    login(client, "alice@example.com")
    return client


def flat(html: str) -> str:
    return " ".join(html.split())


# ------------------------------------------------------------------ pages and access
@pytest.mark.parametrize("path", ["/settings/preferences", "/settings/defaults", "/settings/privacy"])
def test_pages_render_for_the_owner_and_need_login(ac, client, path):
    r = ac.get(path)
    assert r.status_code == 200 and 'aria-label="Settings sections"' in r.get_data(as_text=True)
    assert "<script>" not in r.get_data(as_text=True)
    anon = client.application.test_client()
    assert anon.get(path).status_code in (302, 401)


def test_unverified_users_cannot_use_settings(client, app):
    make_user("new@example.com", verified=False)
    login(client, "new@example.com")
    for path in ("/settings/preferences", "/settings/defaults", "/settings/privacy"):
        assert client.get(path).status_code in (302, 403)


# ------------------------------------------------------------------ preferences
def test_theme_and_notification_preferences_are_saved(ac, alice):
    r = post_form(ac, "/settings/preferences", {"theme_preference": "dark", "notify_on_finish": "y"})
    assert r.status_code == 302
    db.session.refresh(alice)
    assert alice.theme_preference == "dark" and alice.notify_on_finish is True
    assert 'data-theme="dark"' in ac.get("/dashboard").get_data(as_text=True)
    post_form(ac, "/settings/preferences", {"theme_preference": "system"})
    db.session.refresh(alice)
    assert (
        alice.notify_on_finish is False
        and "data-theme=" not in ac.get("/dashboard").get_data(as_text=True).split(">")[1]
    )
    assert db.session.scalars(select(AuditEvent).where(AuditEvent.event_type == "preferences_changed")).all()


def test_invalid_theme_is_rejected(ac, alice):
    r = post_form(ac, "/settings/preferences", {"theme_preference": "<script>"})
    assert r.status_code == 400
    db.session.refresh(alice)
    assert alice.theme_preference == "system"


def test_notifications_default_to_off(alice):
    assert (
        alice.notify_on_finish is False
        and alice.theme_preference == "system"
        and alice.default_settings is None
    )


# ------------------------------------------------------------------ report defaults
DEFAULTS = {"depth": "deep", "output_length": "long", "audience": "executives", "language": "ur",
            "writing_style": "brief", "prioritize_domains": "nature.com\nwho.int", "exclude_domains": "",
            "max_sources": "", "max_research_rounds": "", "max_revision_rounds": "", "output_format": "docx",
            "citation_style": "apa", "enable_critic": "y"}  # fmt: skip


def test_defaults_are_saved_and_prefill_the_new_project_form(ac, alice):
    r = post_form(ac, "/settings/defaults", {**DEFAULTS, "source_categories": ["academic", "government"]})
    assert r.status_code == 302
    db.session.refresh(alice)
    assert alice.default_settings["depth"] == "deep" and alice.default_settings["prioritize_domains"] == [
        "nature.com",
        "who.int",
    ]
    assert alice.default_settings["enable_additional_research"] is False  # unticked box stays unticked
    html = flat(ac.get("/research/new").get_data(as_text=True))
    assert re.search(r'<option[^>]*selected[^>]*value="deep"|<option[^>]*value="deep"[^>]*selected', html)
    assert 'value="executives"' in html and "nature.com" in html and "who.int" in html
    assert re.search(r'value="academic"[^>]*checked|checked[^>]*value="academic"', html)
    # the defaults page shows what was saved
    page = flat(ac.get("/settings/defaults").get_data(as_text=True))
    assert 'value="executives"' in page and "Clear my defaults" in page


def test_invalid_defaults_are_not_stored(ac, alice):
    r = post_form(ac, "/settings/defaults", {**DEFAULTS, "max_sources": "5000"})
    assert r.status_code == 400
    r = post_form(ac, "/settings/defaults", {**DEFAULTS, "prioritize_domains": "not a domain!"})
    assert r.status_code == 400
    db.session.refresh(alice)
    assert alice.default_settings is None


def test_defaults_only_prefill_and_other_users_keep_theirs(ac, client, alice, bob):
    post_form(ac, "/settings/defaults", DEFAULTS)
    other = client.application.test_client()
    login(other, "bob@example.com")
    assert 'value="executives"' not in flat(other.get("/research/new").get_data(as_text=True))
    # a project is still validated from what is submitted, not from the defaults
    r = post_form(ac, "/research/new", {**DEFAULTS, "title": "T", "topic": "How do heat pumps work in cold places?",
                                         "objectives": "", "date_from": "", "date_to": ""})  # fmt: skip
    assert r.status_code == 302
    project = db.session.scalars(select(ResearchProject)).one()
    assert project.settings["audience"] == "executives" and project.settings["language"] == "ur"


def test_reset_defaults(ac, alice):
    post_form(ac, "/settings/defaults", DEFAULTS)
    post_form(ac, "/settings/defaults/reset", {}, csrf_from="/settings/defaults")
    db.session.refresh(alice)
    assert alice.default_settings is None


# ------------------------------------------------------------------ finish notifications
def finished_job(user, status="completed"):
    g = build(user)
    g["job"].status = status
    db.session.commit()
    return g


def test_finish_email_only_for_opted_in_users(app, alice, outbox):
    g = finished_job(alice)
    assert account.notify_job_finished(app, g["job"].public_id) is False and not outbox  # default: off
    alice.notify_on_finish = True
    db.session.commit()
    assert account.notify_job_finished(app, g["job"].public_id) is True
    assert len(outbox) == 1 and outbox[0]["To"] == "alice@example.com"
    body = outbox[0].get_body(("plain",)).get_content()
    assert f"http://localhost/research/{g['project'].public_id}" in body and "Project a" in body
    assert "COP" not in body  # no report text in the email


def test_no_email_while_active_or_when_deletion_pending(app, alice, outbox):
    alice.notify_on_finish = True
    g = finished_job(alice, status="writing")
    assert account.notify_job_finished(app, g["job"].public_id) is False
    g["job"].status = "completed"
    alice.deletion_requested_at = utcnow()
    db.session.commit()
    assert account.notify_job_finished(app, g["job"].public_id) is False and not outbox


def test_notification_failure_never_raises(app, alice, monkeypatch):
    alice.notify_on_finish = True
    g = finished_job(alice)
    monkeypatch.setattr(
        "core.mailer.send_email", lambda **kw: (_ for _ in ()).throw(RuntimeError("smtp down"))
    )
    assert account.notify_job_finished(app, g["job"].public_id) is False


def test_runner_calls_the_notifier_after_a_run(app, alice, monkeypatch):
    import jobs
    from agents import runner

    g = build(alice, job_status="queued")
    seen = []
    monkeypatch.setattr(runner, "default_context", lambda app_, job_id: job_id)
    monkeypatch.setattr(runner, "run_job", lambda job_id: None)
    monkeypatch.setattr(jobs, "claim", lambda job_id, stale: True)
    monkeypatch.setattr("settings.services.notify_job_finished", lambda app_, pid: seen.append(pid))
    assert jobs.run_by_public_id(app, g["job"].public_id) is True
    assert seen == [g["job"].public_id]


# ------------------------------------------------------------------ data export
def test_export_contains_own_data_only_and_no_secrets(ac, alice, bob):
    g = finish(build(alice))
    build(bob, "secret")
    ProviderUsage_row = ProviderUsage(user_id=alice.id, job_id=g["job"].id, request_id="r1", task="writer", provider="gemini",
                                      outcome="ok", latency_ms=5, prompt_tokens=3, completion_tokens=4)  # fmt: skip
    db.session.add(ProviderUsage_row)
    db.session.commit()
    r = post_form(ac, "/settings/privacy/export", {}, csrf_from="/settings/privacy")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    assert r.headers["Content-Disposition"].startswith('attachment; filename="researchpilot-export-')
    z = zipfile.ZipFile(io.BytesIO(r.data))
    assert z.testzip() is None
    names = z.namelist()
    assert (
        "README.txt" in names
        and "data.json" in names
        and any(n.startswith("reports/") and n.endswith(".md") for n in names)
    )
    raw = z.read("data.json").decode()
    data = json.loads(raw)
    assert data["account"]["email"] == "alice@example.com" and "password_hash" not in data["account"]
    assert alice.password_hash not in raw and "auth_version" not in data["account"]
    project = data["projects"][0]
    assert (
        project["title"] == "Project a"
        and project["jobs"][0]["sources"]
        and project["jobs"][0]["claims"][0]["sources"]
    )
    assert (
        project["jobs"][0]["sources"][0]["excerpts"]
        and project["reports"][0]["sections"]
        and project["reports"][0]["quality_evaluations"] == []
    )
    assert data["provider_usage"][0]["provider"] == "gemini" and "prompt" not in json.dumps(
        data["provider_usage"][0]
    ).replace("prompt_tokens", "")
    assert "secret" not in raw and "bob@example.com" not in raw
    report_md = next(z.read(n).decode() for n in names if n.startswith("reports/"))
    assert "[1]" in report_md and "[@S" not in report_md
    assert db.session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "data_export_requested")
    ).one()


def test_export_requires_csrf_and_login(ac, client):
    assert ac.post("/settings/privacy/export").status_code == 400
    anon = client.application.test_client()
    assert anon.post("/settings/privacy/export").status_code in (302, 400, 401)


# ------------------------------------------------------------------ account deletion
def request_deletion(client, **overrides):
    data = {"password": PASSWORD, "confirm": account.DELETE_PHRASE, **overrides}
    return post_form(client, "/settings/privacy/delete", data, csrf_from="/settings/privacy")


def test_deletion_needs_password_and_phrase(ac, alice):
    assert request_deletion(ac, password="wrong password!!").status_code == 400
    assert request_deletion(ac, confirm="delete").status_code == 400
    db.session.refresh(alice)
    assert alice.deletion_requested_at is None


def test_deletion_request_stops_runs_blocks_new_ones_and_can_be_cancelled(ac, alice):
    g = build(alice, job_status="researching")
    assert request_deletion(ac).status_code == 302
    db.session.refresh(alice)
    db.session.refresh(g["job"])
    assert alice.deletion_pending and g["job"].cancel_requested_at is not None
    page = flat(ac.get("/settings/privacy").get_data(as_text=True))
    assert "scheduled for deletion" in page and "Cancel deletion" in page
    assert "scheduled for deletion" in ac.get("/dashboard").get_data(as_text=True)
    blocked = post_form(ac, "/research/new", {**DEFAULTS, "title": "T", "topic": "How do heat pumps work in cold places?",
                                               "objectives": "", "date_from": "", "date_to": ""})  # fmt: skip
    assert (
        blocked.status_code == 302
        and not db.session.scalars(select(ResearchProject).where(ResearchProject.title == "T")).all()
    )
    post_form(ac, "/settings/privacy/cancel-deletion", {}, csrf_from="/settings/privacy")
    db.session.refresh(alice)
    assert alice.deletion_requested_at is None
    kinds = [e.event_type for e in db.session.scalars(select(AuditEvent).order_by(AuditEvent.id))]
    assert "account_deletion_requested" in kinds and "account_deletion_cancelled" in kinds


def test_purge_waits_for_the_grace_period_then_removes_everything(app, ac, alice, bob):
    g = finish(build(alice))
    keep = build(bob, "bobs")
    db.session.add(ProviderUsage(user_id=alice.id, job_id=g["job"].id, request_id="r1", task="writer", provider="gemini",
                                 outcome="ok", latency_ms=5))  # fmt: skip
    db.session.commit()
    request_deletion(ac)
    uid = alice.id
    assert account.purge_due_accounts() == []  # grace period still running
    later = utcnow() + timedelta(days=app.config["ACCOUNT_DELETION_GRACE_DAYS"], minutes=1)
    assert account.purge_due_accounts(now=later, dry_run=True) == [uid]
    assert db.session.get(User, uid) is not None
    db.session.expire_all()
    assert account.purge_due_accounts(now=later) == [uid]
    db.session.expire_all()
    assert db.session.get(User, uid) is None
    for model in (ResearchProject, ResearchJob, ResearchSource, ReportDocument):
        assert db.session.scalar(select(func.count()).select_from(model)) >= 1  # only Bob's rows remain
    assert db.session.scalars(select(ResearchProject)).one().id == keep["project"].id
    usage = db.session.scalars(select(ProviderUsage)).one()
    assert usage.user_id is None  # kept for cost accounting, no longer linked
    assert all(e.actor_user_id != uid for e in db.session.scalars(select(AuditEvent)))
    assert db.session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "account_deleted")
    ).one().target_id == str(uid)
    assert db.session.get(User, bob.id) is not None


def test_purge_skips_accounts_with_a_run_still_active(app, alice):
    build(alice, job_status="writing")
    alice.deletion_requested_at = utcnow() - timedelta(days=30)
    db.session.commit()
    assert account.purge_due_accounts() == []
    assert db.session.get(User, alice.id) is not None


def test_purge_cli(app, alice):
    alice.deletion_requested_at = utcnow() - timedelta(days=30)
    db.session.commit()
    runner = app.test_cli_runner()
    assert "Would remove 1 account(s)" in runner.invoke(args=["purge-accounts", "--dry-run"]).output
    assert db.session.get(User, alice.id) is not None
    assert "Removed 1 account(s)" in runner.invoke(args=["purge-accounts"]).output
    db.session.expire_all()
    assert db.session.get(User, alice.id) is None


def test_deleted_users_cannot_sign_in(app, client, alice):
    alice.deletion_requested_at = utcnow() - timedelta(days=30)
    db.session.commit()
    account.purge_due_accounts()
    r = post_form(client, "/auth/login", {"email": "alice@example.com", "password": PASSWORD})
    assert r.status_code in (200, 400, 401) and "dashboard" not in (r.headers.get("Location") or "")
