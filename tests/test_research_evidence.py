"""Uploads through the form, and saving sources/excerpts."""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select

from extensions import db
from research.evidence import (
    mark_failed,
    save_candidates,
    sources_from_uploads,
    store_page,
    store_snippet,
)
from research.models import EvidenceExcerpt, ResearchProject, ResearchSource, ResearchUpload
from research.services import create_job, create_project
from research_tools.extract import extract_html
from research_tools.search import SearchHit
from research_tools.sources import candidates_from_hits
from tests.conftest import get_csrf, login, make_user
from tests.test_research_tools import _docx_bytes, _pdf_bytes
from tests.test_research_ui import VALID


@pytest.fixture()
def alice(app):
    return make_user("alice@example.com", name="Alice")


@pytest.fixture()
def alice_client(client, alice):
    login(client, "alice@example.com")
    return client


def submit_with_files(client, files):
    token = get_csrf(client, "/research/new")
    data = {**VALID, "csrf_token": token}
    data["documents"] = [(io.BytesIO(content), name) for name, content in files]
    return client.post("/research/new", data=data, content_type="multipart/form-data")


def test_form_is_multipart_and_offers_file_input(alice_client):
    html = alice_client.get("/research/new").get_data(as_text=True)
    assert 'enctype="multipart/form-data"' in html and 'type="file"' in html and "multiple" in html


def test_upload_saves_extracted_text_only(alice_client):
    r = submit_with_files(
        alice_client,
        [("notes.txt", b"Heat pumps work well at -15C."), ("r.docx", _docx_bytes()), ("p.pdf", _pdf_bytes())],
    )
    assert r.status_code == 302
    project = db.session.scalar(select(ResearchProject))
    kinds = sorted(u.kind for u in project.uploads)
    assert kinds == ["docx", "pdf", "txt"]
    assert any("Heat pumps" in u.text for u in project.uploads)
    page = alice_client.get(r.headers["Location"]).get_data(as_text=True)
    assert "notes.txt" in page and "Your documents" in page


def test_duplicate_files_are_stored_once(alice_client):
    submit_with_files(alice_client, [("a.txt", b"same content here"), ("b.txt", b"same content here")])
    assert db.session.scalar(select(db.func.count()).select_from(ResearchUpload)) == 1


def test_bad_upload_blocks_project_creation(alice_client):
    r = submit_with_files(alice_client, [("fake.pdf", b"<html>not a pdf</html>")])
    assert r.status_code == 400
    assert "not a valid PDF" in " ".join(r.get_data(as_text=True).split())
    assert db.session.scalar(select(db.func.count()).select_from(ResearchProject)) == 0


def test_too_many_and_wrong_type_rejected(alice_client):
    r = submit_with_files(alice_client, [(f"f{i}.txt", f"content {i}".encode()) for i in range(6)])
    assert r.status_code == 400 and "at most 5" in r.get_data(as_text=True)
    r = submit_with_files(alice_client, [("tool.exe", b"MZ0000")])
    assert r.status_code == 400


def test_oversized_upload_rejected(alice_client, app):
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    r = submit_with_files(alice_client, [("big.txt", b"a" * (3 * 1024 * 1024))])
    assert r.status_code == 413


def test_project_without_files_still_works(alice_client):
    assert submit_with_files(alice_client, []).status_code == 302


def test_deleting_project_deletes_uploads(app, alice):
    project = create_project(alice, title="T", topic="A topic long enough to pass")
    db.session.add(
        ResearchUpload(
            project_id=project.id, filename="a.txt", kind="txt", size_bytes=3, sha256="a" * 64, text="abc"
        )
    )
    db.session.commit()
    db.session.delete(project)
    db.session.commit()
    assert db.session.scalar(select(db.func.count()).select_from(ResearchUpload)) == 0


# ------------------------------------------------------------------ sources and excerpts
@pytest.fixture()
def job(app, alice):
    project = create_project(alice, title="T", topic="Heat pump efficiency in cold climates")
    return create_job(project)


HITS = [
    SearchHit(
        "Heat pump efficiency",
        "https://energy.gov/hp?utm_source=x",
        "Heat pumps keep efficiency in cold climates.",
        0.9,
    ),
    SearchHit("Duplicate", "https://www.energy.gov/hp/", "dup", 0.5),
    SearchHit("Forum thread", "https://reddit.com/r/hvac/1", "my heat pump", 0.4),
    SearchHit("Blog", "https://blog.example.com/x", "heat pump cold", 0.3),
]


def test_candidates_are_deduplicated_ranked_and_capped(job):
    cands = candidates_from_hits(HITS, "heat pump efficiency in cold climates")
    created = save_candidates(job, cands, max_sources=3)
    assert len(created) == 3
    assert [s.rank for s in sorted(created, key=lambda s: s.rank)] == [1, 2, 3]
    assert created[0].domain in {"energy.gov"} or any(s.domain == "energy.gov" for s in created)
    assert all(s.fetch_status == "pending" and s.link_status == "unchecked" for s in created)
    again = save_candidates(job, cands, max_sources=3)
    assert again == []  # already at the cap and already known
    assert db.session.scalar(select(db.func.count()).select_from(ResearchSource)) == 3


def test_snippet_only_sources_are_marked_honestly(job):
    src = save_candidates(job, candidates_from_hits(HITS[:1], "heat pump"), max_sources=5)[0]
    store_snippet(src, HITS[0].snippet)
    store_snippet(src, HITS[0].snippet)  # repeated: no duplicate excerpt
    db.session.commit()
    assert src.fetch_status == "snippet_only" and src.link_status == "unchecked"
    ex = src.excerpts
    assert len(ex) == 1 and ex[0].locator == "search snippet" and ex[0].is_verbatim


def test_fetched_page_stores_verbatim_excerpts(job):
    from tests.fake_web import ARTICLE

    src = save_candidates(job, candidates_from_hits(HITS[:1], "efficiency"), max_sources=5)[0]
    extracted = extract_html(ARTICLE.encode())
    n = store_page(
        src,
        extracted,
        final_url="https://energy.gov/hp",
        http_status=200,
        redirected=False,
        query_text="photovoltaic efficiency",
    )
    db.session.commit()
    assert n >= 1 and src.fetch_status == "retrieved" and src.link_status == "reachable" and src.retrieved_at
    assert src.content_hash and src.published_at.year == 2024
    flat_text = " ".join(extracted.text.split())
    assert all(" ".join(e.text.split()) in flat_text for e in src.excerpts)


def test_failures_map_to_statuses(job):
    srcs = save_candidates(
        job,
        candidates_from_hits([*HITS, SearchHit("Extra", "https://extra.org/p", "x", 0.2)], "heat"),
        max_sources=10,
    )
    mark_failed(srcs[0], reason="unsafe", error="blocked")
    mark_failed(srcs[1], reason="http_error", error="HTTP 403", http_status=403)
    mark_failed(srcs[2], reason="network", error="timed out")
    mark_failed(srcs[3], reason="bad_type", error="unsupported content type", http_status=200)
    db.session.commit()
    assert (srcs[0].fetch_status, srcs[0].link_status) == ("blocked", "unsafe")
    assert (srcs[1].fetch_status, srcs[1].link_status) == ("paywalled", "reachable")
    assert (srcs[2].fetch_status, srcs[2].link_status) == ("failed", "unreachable")
    assert (srcs[3].fetch_status, srcs[3].link_status) == ("failed", "reachable")


def test_uploads_become_sources_with_excerpts(app, alice):
    project = create_project(alice, title="T", topic="Heat pump efficiency in cold climates")
    db.session.add(
        ResearchUpload(project_id=project.id, filename="report.txt", kind="txt", size_bytes=100, sha256="b" * 64,
                       text="Heat pumps keep a coefficient of performance above 2 at minus 15 degrees. " * 5)
    )  # fmt: skip
    db.session.commit()
    job = create_job(project)
    made = sources_from_uploads(job, project, "heat pump performance")
    db.session.commit()
    assert len(made) == 1 and made[0].origin == "upload" and made[0].url is None
    assert db.session.scalar(select(db.func.count()).select_from(EvidenceExcerpt)) >= 1
    assert sources_from_uploads(job, project, "x") == []  # not added twice
