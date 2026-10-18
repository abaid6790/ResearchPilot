"""Phase 9: source explorer and report editor."""

from __future__ import annotations

import hashlib
import re

import pytest
from sqlalchemy import select

from agents.schemas import CritiqueNote, SectionCritique, SectionDraft
from extensions import db
from reports import editing
from reports.models import ReportDocument, ReportRevision, ReportSection
from research.models import ClaimSource, EvidenceExcerpt, ResearchClaim, ResearchSource, ResearchTask
from research.services import create_job, create_project
from tests.conftest import get_csrf, login, make_user
from tests.fake_llm import FakeLLM, respond  # noqa: F401


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build(user, name="a", *, job_status="completed"):
    project = create_project(user, title=f"Project {name}", topic="How do heat pumps work?")
    job = create_job(project)
    job.status = job_status
    job.checkpoint = {"outline": [{"key": "overview", "heading": "Overview", "subtopics": ["Efficiency"]}]}
    task = ResearchTask(job_id=job.id, kind="question", agent="planner", title="Efficiency")
    db.session.add(task)
    db.session.flush()
    srcs = []
    for i, (title, domain, cat, fetch) in enumerate([
        (f"Heat pump guide {name}", "energy.gov", "government", "retrieved"),
        (f"Blog post {name}", "blog.example.com", "blog", "snippet_only"),
        (f"Broken page {name}", "dead.example.com", "other", "failed"),
    ]):  # fmt: skip
        s = ResearchSource(job_id=job.id, task_id=task.id, url=f"https://{domain}/{name}{i}",
                           canonical_url=f"https://{domain}/{name}{i}", url_hash=digest(f"{name}{i}"),
                           domain=domain, title=title, category=cat, fetch_status=fetch, rank=i)  # fmt: skip
        db.session.add(s)
        srcs.append(s)
    db.session.flush()
    ex = EvidenceExcerpt(source_id=srcs[0].id, task_id=task.id, text="COP of modern heat pumps reaches 3 to 4.",
                         content_hash=digest("ex" + name))  # fmt: skip
    claim = ResearchClaim(
        job_id=job.id, task_id=task.id, text="Heat pumps reach a COP of 3 to 4.", confidence=0.8
    )
    db.session.add_all([ex, claim])
    db.session.flush()
    db.session.add(ClaimSource(claim_id=claim.id, source_id=srcs[0].id, job_id=job.id, excerpt_id=ex.id,
                               support_level="direct"))  # fmt: skip
    report = ReportDocument(project_id=project.id, job_id=job.id, title=f"Report {name}", export_ready=True)
    db.session.add(report)
    db.session.flush()
    body = f"Heat pumps reach a COP of 3 to 4 in mild weather [@S{srcs[0].id}]."
    db.session.add_all([
        ReportSection(report_id=report.id, key="overview", heading="Overview", position=0, body=body),
        ReportSection(report_id=report.id, key="references", heading="References", position=1, body=""),
    ])  # fmt: skip
    db.session.commit()
    return {"project": project, "job": job, "report": report, "sources": srcs, "claim": claim}


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


@pytest.fixture()
def g(alice):
    return build(alice)


def section_of(g):
    return db.session.scalar(select(ReportSection).where(ReportSection.report_id == g["report"].id,
                                                         ReportSection.key == "overview"))  # fmt: skip


def url(g, tail=""):
    return f"/research/{g['project'].public_id}{tail}"


def csrf_post(client, g, path, data=None, fetch=False):
    token = re.search(
        r'name="csrf_token" value="([^"]+)"', client.get(url(g, "/report/edit")).get_data(as_text=True)
    ).group(1)
    headers = {"X-Requested-With": "fetch"} if fetch else {}
    return client.post(path, data={**(data or {}), "csrf_token": token}, headers=headers)


# ------------------------------------------------------------------ source explorer
def test_sources_list_shows_real_fields_and_warnings(ac, g):
    html = ac.get(url(g, "/sources")).get_data(as_text=True)
    assert "Heat pump guide a" in html and "energy.gov" in html
    assert "Cited as [1]" in html
    assert "Excerpt directly supports a claim" in html
    assert "Only a search snippet was available" in html
    assert "The page could not be retrieved." in html
    assert "Publication date not stated" in html
    assert 'rel="noopener noreferrer nofollow"' in html


def test_sources_filters_and_search(ac, g):
    assert "Blog post a" not in ac.get(url(g, "/sources?domain=energy.gov")).get_data(as_text=True)
    cat = ac.get(url(g, "/sources?category=blog")).get_data(as_text=True)
    assert "Blog post a" in cat and "Heat pump guide a" not in cat
    ex = ac.get(url(g, "/sources?q=COP of modern")).get_data(as_text=True)
    assert "Heat pump guide a" in ex and "Blog post a" not in ex
    assert "No sources match" in ac.get(url(g, "/sources?q=zzzz")).get_data(as_text=True)
    assert "Heat pump guide a" not in ac.get(url(g, "/sources?q=%25")).get_data(as_text=True)
    cited = ac.get(url(g, "/sources?status=cited")).get_data(as_text=True)
    assert "Heat pump guide a" in cited and "Blog post a" not in cited
    unc = ac.get(url(g, "/sources?status=uncited")).get_data(as_text=True)
    assert "Heat pump guide a" not in unc and "Blog post a" in unc
    prob = ac.get(url(g, "/sources?status=problems")).get_data(as_text=True)
    assert "Broken page a" in prob and "Heat pump guide a" not in prob
    assert ac.get(url(g, "/sources?category=bogus&status=bogus&page=x")).status_code == 200


def test_source_detail(ac, g):
    sid = g["sources"][0].id
    html = ac.get(url(g, f"/sources/{sid}")).get_data(as_text=True)
    assert "COP of modern heat pumps reaches 3 to 4." in html
    assert "Heat pumps reach a COP of 3 to 4." in html
    assert "Overview" in html and "Efficiency" in html
    assert ac.get(url(g, "/sources/999999")).status_code == 404


def test_javascript_urls_are_never_linked(ac, g):
    g["sources"][1].url = "javascript:alert(1)"
    db.session.commit()
    html = ac.get(url(g, f"/sources/{g['sources'][1].id}")).get_data(as_text=True)
    assert 'href="javascript:' not in html
    assert 'href="javascript:' not in ac.get(url(g, "/sources")).get_data(as_text=True)


def test_other_users_cannot_see_sources_or_editor(client, bob, g):
    login(client, "bob@example.com")
    for tail in ("/sources", f"/sources/{g['sources'][0].id}", "/report/edit", "/report/text"):
        assert client.get(url(g, tail)).status_code == 404
    token = get_csrf(client, "/dashboard")
    r = client.post(
        url(g, "/report/edit/overview/save"), data={"body": "x", "version": 1, "csrf_token": token}
    )
    assert r.status_code == 404
    assert section_of(g).body.startswith("Heat pumps")


def test_source_of_another_project_is_404(ac, alice, g):
    other = build(alice, "b")
    assert ac.get(url(g, f"/sources/{other['sources'][0].id}")).status_code == 404


# ------------------------------------------------------------------ editor: saving
def test_editor_page_renders(ac, g):
    html = ac.get(url(g, "/report/edit")).get_data(as_text=True)
    assert (
        "Edit report" in html
        and "Overview" in html
        and "References" not in html.split("Outline")[1].split("</nav>")[0]
    )
    assert "<script>" not in html and " onclick=" not in html
    assert ac.get(url(g, "/report/edit?section=nope")).status_code == 404


def test_save_creates_revision_and_clears_export_ready(ac, g):
    sid = g["sources"][0].id
    r = csrf_post(ac, g, url(g, "/report/edit/overview/save"),
                  {"body": f"Edited text [@S{sid}].", "version": section_of(g).version})  # fmt: skip
    assert r.status_code == 302
    db.session.expire_all()
    sec = section_of(g)
    assert sec.body == f"Edited text [@S{sid}]." and sec.version == 2 and sec.edited_by_user
    report = db.session.get(ReportDocument, g["report"].id)
    assert report.export_ready is False
    revs = db.session.scalars(select(ReportRevision).where(ReportRevision.report_id == report.id)).all()
    assert [(x.source, x.change_summary) for x in revs] == [("user_edit", "Saved by you")]


def test_stale_version_is_a_conflict_and_nothing_is_overwritten(ac, g):
    path = url(g, "/report/edit/overview/save")
    assert csrf_post(ac, g, path, {"body": "first", "version": 1}, fetch=True).status_code == 200
    r = csrf_post(ac, g, path, {"body": "second", "version": 1}, fetch=True)
    assert r.status_code == 409 and r.get_json()["error"] == "conflict"
    db.session.expire_all()
    assert section_of(g).body == "first"


def test_autosave_keeps_one_rolling_revision_and_reports_checks(ac, g):
    path = url(g, "/report/edit/overview/save")
    r1 = csrf_post(ac, g, path, {"body": "draft one [@S999999]", "version": 1, "autosave": "1"}, fetch=True)
    assert r1.status_code == 200
    data = r1.get_json()
    assert data["version"] == 2 and data["checks"]["invalid_citations"] == [999999]
    r2 = csrf_post(ac, g, path, {"body": "draft two", "version": 2, "autosave": "1"}, fetch=True)
    assert r2.get_json()["version"] == 3
    revs = db.session.scalars(select(ReportRevision).where(ReportRevision.report_id == g["report"].id)).all()
    assert len(revs) == 1 and revs[0].content == "draft two"
    csrf_post(ac, g, path, {"body": "final", "version": 3})
    assert (
        len(
            db.session.scalars(select(ReportRevision).where(ReportRevision.report_id == g["report"].id)).all()
        )
        == 2
    )


def test_unverified_quote_and_uncited_paragraph_are_flagged(ac, g):
    sid = g["sources"][0].id
    quote = f'The report said "heat pumps will replace every furnace by next year" [@S{sid}].'
    r = csrf_post(ac, g, url(g, "/report/edit/overview/save"), {"body": quote, "version": 1}, fetch=True)
    assert r.get_json()["checks"]["unverified_quotes"]
    long = "x" * 130
    r = csrf_post(ac, g, url(g, "/report/edit/overview/save"), {"body": long, "version": 2}, fetch=True)
    assert r.get_json()["checks"]["uncited_paragraphs"] == 1


def test_references_section_and_oversize_are_refused(ac, g):
    assert (
        csrf_post(
            ac, g, url(g, "/report/edit/references/save"), {"body": "x", "version": 1}, fetch=True
        ).status_code
        == 403
    )
    big = csrf_post(
        ac, g, url(g, "/report/edit/overview/save"), {"body": "a" * 20001, "version": 1}, fetch=True
    )
    assert big.status_code == 422
    assert section_of(g).body.startswith("Heat pumps")


def test_editing_paused_while_run_is_active(ac, alice):
    g2 = build(alice, "c", job_status="writing")
    token = re.search(
        r'name="csrf_token" value="([^"]+)"', ac.get(url(g2, "/report/edit")).get_data(as_text=True)
    ).group(1)
    r = ac.post(
        url(g2, "/report/edit/overview/save"),
        data={"body": "z", "version": 1, "csrf_token": token},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 409 and r.get_json()["error"] == "busy"


def test_csrf_is_required(ac, g):
    r = ac.post(url(g, "/report/edit/overview/save"), data={"body": "x", "version": 1})
    assert r.status_code == 400


def test_control_characters_are_stripped(ac, g):
    csrf_post(ac, g, url(g, "/report/edit/overview/save"), {"body": "a\x00b\x07c\r\nd", "version": 1})
    db.session.expire_all()
    assert section_of(g).body == "abc\nd"


def test_markup_in_text_is_escaped_in_editor_and_report(ac, g):
    payload = "<img src=x onerror=alert(1)> <script>alert(1)</script>"
    csrf_post(ac, g, url(g, "/report/edit/overview/save"), {"body": payload, "version": 1})
    for tail in ("/report/edit", "/report"):
        html = ac.get(url(g, tail)).get_data(as_text=True)
        assert "<script>alert" not in html and "<img src=x" not in html


# ------------------------------------------------------------------ history, compare, restore
def test_compare_and_restore(ac, g):
    path = url(g, "/report/edit/overview/save")
    csrf_post(ac, g, path, {"body": "Version two text", "version": 1})
    csrf_post(ac, g, path, {"body": "Version three text", "version": 2})
    revs = db.session.scalars(
        select(ReportRevision)
        .where(ReportRevision.report_id == g["report"].id)
        .order_by(ReportRevision.revision_no)
    ).all()
    html = ac.get(url(g, f"/report/edit?section=overview&compare={revs[0].id}")).get_data(as_text=True)
    assert (
        "compared with the current text" in html
        and "Version two text" in html
        and "Version three text" in html
    )
    r = csrf_post(ac, g, url(g, f"/report/revisions/{revs[0].id}/restore"), {"version": 3})
    assert r.status_code == 302
    db.session.expire_all()
    assert section_of(g).body == "Version two text"
    last = db.session.scalars(
        select(ReportRevision)
        .where(ReportRevision.report_id == g["report"].id)
        .order_by(ReportRevision.revision_no.desc())
    ).first()
    assert last.source == "restore" and last.status == "applied"
    # a stale restore changes nothing
    csrf_post(ac, g, url(g, f"/report/revisions/{revs[1].id}/restore"), {"version": 1})
    db.session.expire_all()
    assert section_of(g).body == "Version two text"


def test_revision_of_another_report_is_404(ac, alice, g):
    other = build(alice, "b")
    csrf_post(ac, other, url(other, "/report/edit/overview/save"), {"body": "other", "version": 1})
    rev = db.session.scalars(
        select(ReportRevision).where(ReportRevision.report_id == other["report"].id)
    ).one()
    r = csrf_post(ac, g, url(g, f"/report/revisions/{rev.id}/restore"), {"version": 1})
    assert r.status_code == 404


# ------------------------------------------------------------------ regeneration and critique
def install_llm(app, llm):
    app.extensions["llm_router"] = llm
    return llm


def test_regeneration_is_a_proposal_until_applied(app, ac, g):
    sid = g["sources"][0].id
    llm = install_llm(
        app, FakeLLM(writer=lambda req, self: SectionDraft(body=f"Rewritten clearly [@S{sid}] [@S424242]."))
    )
    original = section_of(g).body
    r = csrf_post(ac, g, url(g, "/report/edit/overview/regenerate"), {"instruction": "make it clearer"})
    assert r.status_code == 302 and llm.count("writer") == 1
    db.session.expire_all()
    assert section_of(g).body == original  # nothing changed yet
    prop = db.session.scalars(select(ReportRevision).where(ReportRevision.status == "proposed")).one()
    assert "[@S424242]" not in prop.content and prop.structure["removed_citations"] == [424242]
    html = ac.get(url(g, "/report/edit")).get_data(as_text=True)
    assert "Rewritten clearly" in html and "Apply" in html and "Discard" in html
    # untrusted text is wrapped when sent to the model
    assert "BEGIN" in llm.calls[0][2].upper() or "claims" in llm.calls[0][2]

    r = csrf_post(ac, g, url(g, f"/report/revisions/{prop.id}/apply"))
    assert r.status_code == 302
    db.session.expire_all()
    assert section_of(g).body.startswith("Rewritten clearly")
    assert db.session.get(ReportRevision, prop.id).status == "applied"
    assert db.session.get(ReportDocument, g["report"].id).export_ready is False


def test_discard_leaves_text_and_keeps_record(app, ac, g):
    install_llm(app, FakeLLM())
    original = section_of(g).body
    csrf_post(ac, g, url(g, "/report/edit/overview/regenerate"), {})
    prop = db.session.scalars(select(ReportRevision).where(ReportRevision.status == "proposed")).one()
    csrf_post(ac, g, url(g, f"/report/revisions/{prop.id}/discard"))
    db.session.expire_all()
    assert section_of(g).body == original and db.session.get(ReportRevision, prop.id).status == "discarded"


def test_proposal_goes_stale_after_an_edit(app, ac, g):
    install_llm(app, FakeLLM())
    csrf_post(ac, g, url(g, "/report/edit/overview/regenerate"), {})
    prop = db.session.scalars(select(ReportRevision).where(ReportRevision.status == "proposed")).one()
    csrf_post(ac, g, url(g, "/report/edit/overview/save"), {"body": "my own words", "version": 1})
    csrf_post(ac, g, url(g, f"/report/revisions/{prop.id}/apply"))
    db.session.expire_all()
    assert (
        section_of(g).body == "my own words" and db.session.get(ReportRevision, prop.id).status == "proposed"
    )


def test_regeneration_failures_are_reported_not_applied(app, ac, g):
    llm = install_llm(app, FakeLLM())
    llm.fail_tasks.add("writer")
    r = csrf_post(ac, g, url(g, "/report/edit/overview/regenerate"), {})
    assert r.status_code == 302
    assert not db.session.scalars(select(ReportRevision)).all()
    assert "could answer right now" in ac.get(url(g, "/report/edit")).get_data(as_text=True)


def test_regeneration_needs_evidence(app, ac, g):
    install_llm(app, FakeLLM())
    db.session.query(ResearchClaim).delete()
    db.session.commit()
    csrf_post(ac, g, url(g, "/report/edit/overview/regenerate"), {})
    assert not db.session.scalars(select(ReportRevision)).all()


def test_critique_returns_notes_without_changing_text(app, ac, g):
    install_llm(app, FakeLLM(critic=lambda req, self: SectionCritique(
        assessment="Overstated.", notes=[CritiqueNote(severity="major", problem="Too strong", suggestion="Soften")])))  # fmt: skip
    original = section_of(g).body
    r = csrf_post(ac, g, url(g, "/report/edit/overview/critique"), fetch=True)
    assert r.status_code == 200 and r.get_json()["notes"][0]["problem"] == "Too strong"
    db.session.expire_all()
    assert section_of(g).body == original and not db.session.scalars(select(ReportRevision)).all()


# ------------------------------------------------------------------ copy text and pages
def test_copy_text_uses_numbers_and_lists_references(ac, g):
    r = ac.get(url(g, "/report/text"))
    text = r.get_data(as_text=True)
    assert r.mimetype == "text/plain" and "[@S" not in text
    assert (
        "COP of 3 to 4 in mild weather [1]." in text
        and "## References" in text
        and "[1] Heat pump guide a" in text
    )


def test_report_and_detail_pages_link_to_new_tools(ac, g):
    html = ac.get(url(g, "/report")).get_data(as_text=True)
    assert "Edit report" in html and "Source explorer" in html and "Inspect source" in html
    assert "editing comes later" not in html


def test_diff_lines_unit():
    lines = editing.diff_lines("a\nb\nc", "a\nB\nc")
    assert [x["op"] for x in lines] == ["same", "del", "add", "same"]


def test_audit_events_recorded_for_manual_save_not_autosave(ac, g):
    from core.audit import AuditEvent

    csrf_post(
        ac,
        g,
        url(g, "/report/edit/overview/save"),
        {"body": "one", "version": 1, "autosave": "1"},
        fetch=True,
    )
    assert not db.session.scalars(select(AuditEvent).where(AuditEvent.event_type == "report_edited")).all()
    csrf_post(ac, g, url(g, "/report/edit/overview/save"), {"body": "two", "version": 2})
    assert (
        len(db.session.scalars(select(AuditEvent).where(AuditEvent.event_type == "report_edited")).all()) == 1
    )
