"""Runs one research job to completion (synchronously). Phase 7 calls this from a Celery task."""

from __future__ import annotations

import logging
import os

from agents.context import AgentContext, AgentFailure, Cancelled
from agents.graph import build_graph
from core.db_utils import utcnow
from core.logging_utils import redact
from extensions import db
from research import constants as c
from research.models import ResearchJob
from research.services import append_event

logger = logging.getLogger(__name__)
RECURSION_LIMIT = 80  # more than any legal path through the graph; stops runaway loops


def default_context(app, job_id: int) -> AgentContext:
    from providers import get_router
    from research_tools import get_search_client
    from research_tools.fetch import FetchSettings, SafeFetcher

    settings = FetchSettings.from_env(
        app.config.get("TOOLS_ENVIRON") or ({} if app.config.get("TESTING") else os.environ)
    )
    return AgentContext(
        job_id=job_id, router=get_router(app), search=get_search_client(app), fetcher_factory=lambda: SafeFetcher(settings),
        max_llm_calls=int(os.environ.get("AGENT_MAX_LLM_CALLS", "120") or 120),
    )  # fmt: skip


def run_job(ctx: AgentContext) -> ResearchJob:
    job = ctx.job
    if job.status not in c.JOB_ACTIVE_STATUSES:
        return job
    if job.started_at is None:
        job.started_at = utcnow()
    job.error_code = job.error_message = None
    db.session.commit()
    try:
        build_graph(ctx).invoke({}, {"recursion_limit": RECURSION_LIMIT})
    except Cancelled:
        _close(
            ctx,
            c.JOB_CANCELLED,
            "cancelled",
            "Stopped at your request. Partial results are kept.",
            level="warning",
        )
    except AgentFailure as exc:
        extra = {"retry_at": exc.retry_at} if exc.retry_at else None
        _close(ctx, c.JOB_FAILED, exc.code, exc.message, level="error", data=extra)
    except Exception as exc:  # noqa: BLE001 - a bug must not leave the job "running" forever
        logger.exception("Research job %s crashed: %s", ctx.job_id, redact(str(exc))[:300])
        _close(
            ctx,
            c.JOB_FAILED,
            "internal_error",
            "Something went wrong inside the run. Partial results are kept.",
            level="error",
        )
    return ctx.job


def _close(
    ctx: AgentContext, status: str, code: str, message: str, *, level: str, data: dict | None = None
) -> None:
    db.session.rollback()
    job = ctx.job
    ctx.finish(status, error_code=code, error_message=message)
    append_event(job, f"job_{status}", message, level=level, stage=status, agent="system", data=data)
    db.session.commit()
