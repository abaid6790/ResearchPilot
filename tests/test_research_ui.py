"""Research workspace: form validation, saving, listing, ownership, deletion, dashboard."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from core.audit import AuditEvent
from extensions import db
from research.models import ResearchProject
from research.services import create_job, create_project
from tests.conftest import get_csrf, login, make_user, post_form

VALID = {
    "title": "Heat pumps",
    "topic": "How efficient are modern air-source heat pumps in cold climates?",
    "objectives": "",
    "depth": "standard",
    "output_length": "medium",
    "audience": "general",
    "language": "en",
    "writing_style": "neutral",
    "prioritize_domains": "",
    "exclude_domains": "",
    "date_from": "",
    "date_to": "",
    "max_sources": "",
    "max_research_rounds": "",
    "max_revision_rounds": "",
    "output_format": "pdf",
    "citation_style": "numeric",
    "enable_critic": "y",
    "enable_additional_research": "y",
}


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


def flat(html: str) -> str:
    """Collapse whitespace so assertions do not depend on template line breaks."""
    return " ".join(html.split())


def submit(client, **overrides):
    data = {**VALID, **overrides}
    return post_form(client, "/research/new", data, csrf_from="/research/new")


def test_pages_require_login(client):
    for path in ("/research/", "/research/new", "/research/abc"):
        assert client.get(path).status_code in (302, 401)


def test_unverified_users_are_sent_to_verification(client, app):
    make_user("new@example.com", verified=False)
    login(client, "new@example.com")
    assert client.get("/research/new").status_code == 302


def test_new_form_renders_with_all_spec_fields(alice_client):
    html = alice_client.get("/research/new").get_data(as_text=True)
    for name in ("title", "topic", "objectives", "depth", "output_length", "audience", "language",
                 "writing_style", "source_categories", "prioritize_domains", "exclude_domains", "date_from",
                 "date_to", "max_sources", "max_research_rounds", "max_revision_rounds", "output_format",
                 "citation_style", "enable_critic", "enable_additional_research"):  # fmt: skip
        assert f'name="{name}"' in html, name
    assert "uses more time and API quota" in html


def test_valid_submission_saves_project_with_validated_settings(alice_client, alice):
    response = submit(alice_client, depth="deep", exclude_domains="HTTPS://www.Spam.com/x\nads.example.org",
                      source_categories="academic")  # fmt: skip
    assert response.status_code == 302
    project = db.session.scalars(select(ResearchProject)).one()
    assert response.headers["Location"].endswith(f"/research/{project.public_id}")
    assert project.user_id == alice.id and project.depth == "deep"
    assert project.settings["max_sources"] == 30  # filled from the depth preset
    assert project.settings["exclude_domains"] == ["spam.com", "ads.example.org"]
    assert project.settings["source_categories"] == ["academic"]
    assert project.settings["enable_critic"] is True
    page = alice_client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Heat pumps" in page and "not available in this build" in flat(page)


def test_unchecked_boxes_are_saved_as_off(alice_client):
    data = {k: v for k, v in VALID.items() if k not in ("enable_critic", "enable_additional_research")}
    response = post_form(alice_client, "/research/new", data, csrf_from="/research/new")
    assert response.status_code == 302
    project = db.session.scalars(select(ResearchProject)).one()
    assert (
        project.settings["enable_critic"] is False and project.settings["enable_additional_research"] is False
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"title": ""}, "required"),
        ({"topic": "short"}, "between 10"),
        ({"topic": "x" * 4001}, "between 10"),
        ({"max_sources": "5000"}, "100"),
        ({"max_research_rounds": "0"}, "1"),
        ({"max_revision_rounds": "99"}, "5"),
        ({"exclude_domains": "not a domain"}, "not a valid domain"),
        ({"prioritize_domains": "a.com", "exclude_domains": "a.com"}, "both prioritised and excluded"),
        ({"date_from": "2026-05-01", "date_to": "2026-01-01"}, "date_from"),
        ({"depth": "extreme"}, "Not a valid choice"),
        ({"language": "klingon"}, "Not a valid choice"),
    ],
)
def test_invalid_input_is_rejected_with_a_message_and_nothing_saved(alice_client, overrides, message):
    response = submit(alice_client, **overrides)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    assert db.session.scalars(select(ResearchProject)).first() is None


def test_form_keeps_what_the_user_typed_after_an_error(alice_client):
    html = submit(alice_client, title="My title", topic="short").get_data(as_text=True)
    assert 'value="My title"' in html


def test_script_in_input_is_escaped_everywhere(alice_client):
    payload = "<script>alert(1)</script>"
    response = submit(alice_client, title=payload, topic=f"{payload} is a long enough topic")
    page = alice_client.get(response.headers["Location"]).get_data(as_text=True)
    assert payload not in page and "&lt;script&gt;" in page
    assert payload not in alice_client.get("/research/").get_data(as_text=True)
    assert payload not in alice_client.get("/dashboard").get_data(as_text=True)


def test_csrf_is_required_to_create_and_delete(alice_client, alice):
    assert alice_client.post("/research/new", data=VALID).status_code == 400
    project = create_project(alice, title="t", topic="a topic long enough")
    assert (
        alice_client.post(f"/research/{project.public_id}/delete", data={"confirm_title": "t"}).status_code
        == 400
    )
    assert db.session.get(ResearchProject, project.id) is not None


def test_list_shows_only_own_projects_and_search_works(alice_client, alice, bob):
    create_project(alice, title="Solar panels", topic="photovoltaic efficiency trends")
    create_project(alice, title="Wind farms", topic="offshore turbines")
    create_project(bob, title="Bob secret", topic="private")
    html = alice_client.get("/research/").get_data(as_text=True)
    assert "Solar panels" in html and "Wind farms" in html and "Bob secret" not in html
    found = alice_client.get("/research/?q=PHOTOVOLTAIC").get_data(as_text=True)
    assert "Solar panels" in found and "Wind farms" not in found
    assert "No projects match" in alice_client.get("/research/?q=zzz").get_data(as_text=True)
    assert "Wind farms" not in alice_client.get("/research/?q=%25").get_data(as_text=True)  # % is literal


def test_list_pagination(alice_client, alice):
    for i in range(15):
        create_project(alice, title=f"Project {i:02d}", topic="a topic long enough")
    first = alice_client.get("/research/").get_data(as_text=True)
    assert "Page 1 of 2" in first and "Project 14" in first and "Project 00" not in first
    assert "Project 00" in alice_client.get("/research/?page=2").get_data(as_text=True)
    assert alice_client.get("/research/?page=abc").status_code == 200


def test_other_users_project_is_a_404_for_every_action(alice_client, bob):
    project = create_project(bob, title="Bob secret", topic="private topic here")
    pid = project.public_id
    assert alice_client.get(f"/research/{pid}").status_code == 404
    token = get_csrf(alice_client, "/research/new")
    for action in ("delete", "archive"):
        response = alice_client.post(
            f"/research/{pid}/{action}", data={"csrf_token": token, "confirm_title": "Bob secret"}
        )
        assert response.status_code == 404
    assert db.session.get(ResearchProject, project.id).archived_at is None
    assert alice_client.get("/research/0000").status_code == 404


def test_archive_and_restore(alice_client, alice):
    project = create_project(alice, title="Archive me", topic="a topic long enough")
    token = get_csrf(alice_client, "/research/new")
    r = alice_client.post(f"/research/{project.public_id}/archive", data={"csrf_token": token})
    assert r.status_code == 302
    assert "Archive me" not in alice_client.get("/research/").get_data(as_text=True)
    assert "Archive me" in alice_client.get("/research/?show=archived").get_data(as_text=True)
    alice_client.post(f"/research/{project.public_id}/archive", data={"csrf_token": token})
    assert "Archive me" in alice_client.get("/research/").get_data(as_text=True)


def test_delete_requires_the_typed_title_and_is_audited(alice_client, alice):
    project = create_project(alice, title="Delete me", topic="a topic long enough")
    token = get_csrf(alice_client, "/research/new")
    url = f"/research/{project.public_id}/delete"
    alice_client.post(url, data={"csrf_token": token, "confirm_title": "wrong"})
    assert db.session.get(ResearchProject, project.id) is not None
    response = alice_client.post(url, data={"csrf_token": token, "confirm_title": "Delete me"})
    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(ResearchProject, project.id) is None
    event = db.session.scalars(select(AuditEvent).where(AuditEvent.event_type == "project_deleted")).one()
    assert event.actor_user_id == alice.id and event.target_id == project.public_id


def test_project_with_an_active_job_cannot_be_deleted(alice_client, alice):
    project = create_project(alice, title="Busy", topic="a topic long enough")
    create_job(project)
    token = get_csrf(alice_client, "/research/new")
    alice_client.post(
        f"/research/{project.public_id}/delete", data={"csrf_token": token, "confirm_title": "Busy"}
    )
    assert db.session.get(ResearchProject, project.id) is not None


def test_dashboard_counts_are_real_and_per_user(alice_client, alice, bob):
    empty = alice_client.get("/dashboard").get_data(as_text=True)
    assert "No research projects yet" in empty
    create_project(alice, title="Mine one", topic="a topic long enough")
    create_job(create_project(alice, title="Mine two", topic="a topic long enough"))
    create_project(bob, title="Not mine", topic="private topic here")
    html = alice_client.get("/dashboard").get_data(as_text=True)
    assert "Mine one" in html and "Mine two" in html and "Not mine" not in html
    stats = html.split('class="stat__value">')
    assert [s.split("<")[0] for s in stats[1:4]] == ["2", "1", "0"]  # projects, running, reports


def test_detail_page_shows_runs_and_is_honest_about_run_support(alice_client, alice):
    project = create_project(alice, title="With job", topic="a topic long enough")
    create_job(project)
    html = alice_client.get(f"/research/{project.public_id}").get_data(as_text=True)
    assert "A run is in progress" in html and "not available in this build" in flat(html)
    assert "Start research" not in html  # no fake button


def test_security_headers_and_no_inline_scripts_on_new_pages(alice_client, alice):
    project = create_project(alice, title="CSP", topic="a topic long enough")
    for path in ("/research/", "/research/new", f"/research/{project.public_id}", "/dashboard"):
        response = alice_client.get(path)
        assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        html = response.get_data(as_text=True)
        assert "<script>" not in html and " onclick=" not in html and ' style="' not in html
