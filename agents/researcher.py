"""Researcher: runs searches, saves candidate sources, fetches pages and stores verbatim excerpts.

Only this module's ordinary code touches the network; no model decides what to fetch beyond the
search queries it proposed, and every URL passes through the SSRF-safe fetcher.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from agents.context import AgentContext, AgentFailure
from core.db_utils import utcnow
from extensions import db
from providers.base import ErrorKind
from research.evidence import (
    mark_failed,
    save_candidates,
    source_count,
    sources_from_uploads,
    store_page,
    store_snippet,
)  # fmt: skip
from research.models import ResearchSource, ResearchTask
from research_tools import sources as S
from research_tools.documents import DocumentError, parse_document
from research_tools.extract import Extracted, decode_bytes, extract_html
from research_tools.search import SearchUnavailableError
from research_tools.untrusted import injection_signals, sanitize_text
from research_tools.urls import domain_matches

MIN_PAGE_CHARS = 200


def _parse(result) -> Extracted:
    if result.kind == "html":
        return extract_html(result.body, result.encoding)
    if result.kind == "pdf":
        doc = parse_document("page.pdf", result.body, max_bytes=len(result.body) + 1)
        return Extracted(text=doc.text, warnings=doc.warnings)
    return Extracted(text=sanitize_text(decode_bytes(result.body, result.encoding)))


def _fetch_one(factory, url: str):
    """Runs in a worker thread: network and parsing only, no database access."""
    result = factory().fetch(url)
    if not result.ok:
        return result, None, None
    try:
        return result, _parse(result), None
    except DocumentError as exc:
        return result, None, str(exc)


def _boost(cands, st) -> None:
    for cand in cands:
        if st.prioritize_domains and domain_matches(cand.domain, st.prioritize_domains):
            cand.quality = min(1.0, round(cand.quality + 0.15, 3))
        if st.source_categories and cand.category not in st.source_categories:
            cand.quality = max(0.0, round(cand.quality - 0.2, 3))  # a heuristic preference, not a filter


def run_research(ctx: AgentContext) -> None:
    job, st = ctx.job, ctx.settings
    job.research_round += 1
    ctx.set_stage("researching", "researcher", f"Searching and reading sources (round {job.research_round}).")
    tasks = list(db.session.scalars(
        select(ResearchTask).where(ResearchTask.job_id == job.id, ResearchTask.status == "pending")
        .order_by(ResearchTask.position)
    ))  # fmt: skip
    if job.research_round == 1:
        made = sources_from_uploads(job, ctx.project, f"{ctx.project.title} {ctx.project.topic}")
        db.session.commit()
        if made:
            ctx.event("source_found", f"Added {len(made)} uploaded document(s) as sources.", stage="researching",
                      agent="researcher", data={"uploads": len(made)})  # fmt: skip

    if not tasks:
        return
    per_task = max(2, math.ceil(st.max_sources / len(tasks)))
    searched_any, search_error = False, None
    for task in tasks:
        ctx.check_cancel()
        task.status, task.attempts, task.started_at = "running", task.attempts + 1, utcnow()
        db.session.commit()
        room = st.max_sources - source_count(job)
        new_sources: list[ResearchSource] = []
        query_text = f"{task.title} {task.description or ''}"
        for query in (task.queries or [])[-3:]:
            if room - len(new_sources) <= 0:
                break
            try:
                response = ctx.search.search(
                    query, max_results=min(8, max(3, per_task)), exclude_domains=st.exclude_domains or None,
                    start_date=st.date_from, end_date=st.date_to, user_id=ctx.project.user_id, job_id=job.id,
                )  # fmt: skip
            except SearchUnavailableError as exc:
                search_error = exc
                ctx.event("warning", f"Search failed for one query ({exc.kind.value}).", level="warning",
                          stage="researching", agent="researcher", data={"kind": exc.kind.value})  # fmt: skip
                if exc.kind in (ErrorKind.AUTH_INVALID, ErrorKind.QUOTA_EXHAUSTED):
                    break  # the same key will fail every query
                continue
            searched_any = True
            cands = S.candidates_from_hits(response.hits, query_text)
            _boost(cands, st)
            cands.sort(key=lambda cand: -cand.score)
            cands = S.diversify(cands, lambda cand: cand.domain, per_task, per_domain=2)
            created = save_candidates(
                job,
                cands,
                max_sources=source_count(job) + min(per_task - len(new_sources), room - len(new_sources)),
                task_id=task.id,
            )  # noqa: E501
            by_url = {h.url: h for h in response.hits}
            for src in created:
                hit = by_url.get(src.url)
                if hit:
                    store_snippet(src, hit.snippet, task_id=task.id)
            new_sources.extend(created)
            db.session.commit()
            ctx.event("query_run", f"Searched: {query[:120]} ({len(response.hits)} results, {len(created)} new).",
                      stage="researching", agent="researcher", data={"results": len(response.hits), "new": len(created)})  # fmt: skip
        _fetch_and_store(ctx, new_sources, query_text, task.id)
        task.status, task.finished_at = "done", utcnow()
        db.session.commit()

    total = source_count(job)
    if total == 0:
        if search_error is not None and not searched_any:
            code = (
                "search_not_configured"
                if search_error.kind is ErrorKind.AUTH_INVALID
                else "search_unavailable"
            )
            raise AgentFailure(code, f"Web search failed: {search_error}")
        raise AgentFailure("no_sources", "No sources were found for this topic. Try broader wording.")
    if not searched_any and job.research_round == 1:
        ctx.event("warning", "Web search was unavailable; the run continues with uploaded documents only.",
                  level="warning", stage="researching", agent="researcher")  # fmt: skip


def _fetch_and_store(ctx: AgentContext, sources: list[ResearchSource], query_text: str, task_id: int) -> None:
    if not sources:
        return
    job_id = ctx.job_id
    with ThreadPoolExecutor(max_workers=ctx.fetch_workers) as pool:
        urls = [s.url for s in sources]  # read in this thread: workers must not touch the database session
        outcomes = list(pool.map(lambda url: _fetch_one(ctx.fetcher_factory, url), urls))
    seen_hashes = {h: i for h, i in db.session.execute(
        select(ResearchSource.content_hash, ResearchSource.id).where(
            ResearchSource.job_id == job_id, ResearchSource.content_hash.is_not(None)))}  # fmt: skip
    ok = failed = 0
    for src, (result, extracted, doc_error) in zip(sources, outcomes, strict=True):
        ctx.check_cancel()
        if doc_error is not None:
            mark_failed(src, reason="bad_type", error=doc_error, http_status=result.status)
            failed += 1
            continue
        if extracted is None:
            mark_failed(
                src,
                reason=result.reason or "network",
                error=result.error or "failed",
                http_status=result.status,
            )
            failed += 1
            continue
        text = extracted.text
        flags = [f"possible prompt injection ({s})" for s in injection_signals(text)]
        digest = S.content_hash(text)
        if len(text) < MIN_PAGE_CHARS:
            src.link_status = "redirected" if result.redirected else "reachable"
            src.http_status = result.status
            src.warnings = [
                *(src.warnings or []),
                "page had too little readable text; only the search snippet is used",
                *flags,
            ]
            continue
        if digest in seen_hashes:
            src.link_status = "redirected" if result.redirected else "reachable"
            src.http_status = result.status
            src.warnings = [
                *(src.warnings or []),
                f"same content as source S{seen_hashes[digest]}; not used twice",
                *flags,
            ]
            src.fetch_status = "snippet_only"
            continue
        store_page(src, extracted, final_url=result.final_url or src.url, http_status=result.status or 200,
                   redirected=result.redirected, query_text=query_text, task_id=task_id)  # fmt: skip
        if flags:
            src.warnings = [*(src.warnings or []), *flags]
        seen_hashes[digest] = src.id
        ok += 1
    db.session.commit()
    ctx.event("source_fetched", f"Read {ok} page(s); {failed} could not be read.", stage="researching",
              agent="researcher", data={"read": ok, "failed": failed})  # fmt: skip
