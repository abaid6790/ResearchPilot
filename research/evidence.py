"""Saving search results, fetched pages and upload text as sources and verbatim excerpts.

URL availability is not verification: ``link_status`` records only that a URL answered. Whether a
source *supports a claim* is judged later, per claim. Excerpts are cut verbatim from the text we
actually retrieved; a search snippet is stored with locator "search snippet" and the source is
marked ``snippet_only`` so nobody mistakes it for a read page.
"""

from __future__ import annotations

from sqlalchemy import func, select

from core.db_utils import utcnow
from extensions import db
from research.models import EvidenceExcerpt, ResearchJob, ResearchSource
from research_tools import sources as S
from research_tools.extract import Extracted
from research_tools.urls import canonicalize, domain_of, url_hash


def source_count(job: ResearchJob) -> int:
    return (
        db.session.scalar(
            select(func.count()).select_from(ResearchSource).where(ResearchSource.job_id == job.id)
        )
        or 0
    )


def save_candidates(
    job: ResearchJob, candidates, *, max_sources: int, task_id: int | None = None
) -> list[ResearchSource]:
    """Insert new candidates (de-duplicated by normalised URL) up to ``max_sources`` for the job.
    Returns only the rows created now."""
    existing = set(db.session.scalars(select(ResearchSource.url_hash).where(ResearchSource.job_id == job.id)))
    room = max_sources - len(existing)
    created: list[ResearchSource] = []
    for cand in candidates:
        if room <= 0:
            break
        try:
            canonical = canonicalize(cand.url)
        except ValueError:
            continue
        digest = url_hash(canonical)
        if digest in existing:
            continue
        existing.add(digest)
        src = ResearchSource(
            job_id=job.id, task_id=task_id, origin="web_search", url=cand.url[:2048], canonical_url=canonical[:2048],
            url_hash=digest, domain=cand.domain[:255], title=(cand.title or cand.domain)[:500],
            published_at=cand.published_at, category=cand.category, fetch_status="pending", link_status="unchecked",
            relevance_score=cand.relevance, quality_score=cand.quality, warnings=cand.warnings or None,
        )  # fmt: skip
        db.session.add(src)
        created.append(src)
        room -= 1
    db.session.flush()
    for rank, src in enumerate(
        sorted(created, key=lambda s: -S.combined(s.relevance_score or 0, s.quality_score or 0))
    ):
        src.rank = rank + 1
    return created


def _add_excerpt(
    source: ResearchSource, text: str, *, locator: str | None, score: float | None, task_id=None
) -> bool:
    digest = S.content_hash(text)
    exists = db.session.scalar(
        select(EvidenceExcerpt.id).where(
            EvidenceExcerpt.source_id == source.id, EvidenceExcerpt.content_hash == digest
        )
    )
    if exists:
        return False
    db.session.add(
        EvidenceExcerpt(source_id=source.id, task_id=task_id, text=text, content_hash=digest, is_verbatim=True,
                        locator=locator, relevance_score=score)
    )  # fmt: skip
    return True


def store_snippet(source: ResearchSource, snippet: str, *, task_id: int | None = None) -> None:
    """Search-provider snippet only; the page itself was not read."""
    snippet = snippet.strip()
    if snippet:
        _add_excerpt(source, snippet, locator="search snippet", score=source.relevance_score, task_id=task_id)
    if source.fetch_status == "pending":
        source.fetch_status = "snippet_only"


def store_page(source: ResearchSource, extracted: Extracted, *, final_url: str, http_status: int, redirected: bool,
               query_text: str, max_excerpts: int = 6, task_id: int | None = None) -> int:  # fmt: skip
    """Record a successfully fetched page and its best passages. Returns the number of excerpts added."""
    source.fetch_status = "retrieved"
    source.link_status = "redirected" if redirected else "reachable"
    source.http_status = http_status
    source.retrieved_at = utcnow()
    source.content_hash = S.content_hash(extracted.text)
    if extracted.title and not source.title:
        source.title = extracted.title[:500]
    if extracted.published_at and not source.published_at:
        source.published_at = extracted.published_at
    if final_url and domain_of(final_url) != source.domain:
        source.warnings = [*(source.warnings or []), f"redirected to {domain_of(final_url)}"]
    source.warnings = [*(source.warnings or []), *extracted.warnings] or None
    added = 0
    for chunk, score in S.rank_chunks(S.chunk_text(extracted.text), query_text, max_excerpts):
        added += _add_excerpt(source, chunk, locator=None, score=score, task_id=task_id)
    source.quality_score = S.quality(
        source.url or "", source.category, source.published_at, len(extracted.text)
    )
    return added


def mark_failed(source: ResearchSource, *, reason: str, error: str, http_status: int | None = None) -> None:
    source.http_status = http_status
    source.warnings = [*(source.warnings or []), f"fetch failed: {error}"]
    if reason == "unsafe":
        source.fetch_status, source.link_status = "blocked", "unsafe"
    elif http_status in (401, 402, 403, 451):
        source.fetch_status, source.link_status = "paywalled", "reachable"
    elif reason in ("bad_type", "too_large") and http_status and http_status < 400:
        source.fetch_status, source.link_status = "failed", "reachable"
    else:
        source.fetch_status, source.link_status = "failed", "unreachable"


def sources_from_uploads(
    job: ResearchJob, project, query_text: str, *, max_excerpts: int = 8
) -> list[ResearchSource]:
    """Turn a project's uploaded documents into sources for the job (no URL, ``origin='upload'``)."""
    out = []
    already = set(
        db.session.scalars(select(ResearchSource.upload_name).where(ResearchSource.job_id == job.id))
    )
    for up in project.uploads:
        if up.filename in already:
            continue
        src = ResearchSource(
            job_id=job.id, origin="upload", upload_name=up.filename, title=up.filename, category="other",
            fetch_status="retrieved", link_status="unchecked", retrieved_at=utcnow(),
            content_hash=S.content_hash(up.text), quality_score=0.5, relevance_score=None, warnings=up.warnings or None,
        )  # fmt: skip
        db.session.add(src)
        db.session.flush()
        for chunk, score in S.rank_chunks(S.chunk_text(up.text), query_text, max_excerpts):
            _add_excerpt(src, chunk, locator=f"{up.kind} upload", score=score)
        out.append(src)
    return out
