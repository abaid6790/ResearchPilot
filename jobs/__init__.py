"""Running research jobs in the background.

Three runners (``JOB_RUNNER``): ``thread`` (a small pool inside the web process; simplest, but jobs
stop if the process restarts), ``celery`` (a separate worker with Redis; the production choice) and
``manual`` (nothing starts by itself; used by tests and ``flask run-research``).

Whatever starts a job, :func:`run_by_public_id` first *claims* it with an atomic database update so
two workers can never run the same job, and the job refreshes a heartbeat while it runs. A job whose
heartbeat has gone stale (its worker died) can be claimed again and resumes where it stopped.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import or_, select, update

from core.db_utils import utcnow
from extensions import db
from research import constants as c
from research.models import ResearchJob

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def claim(job_id: int, stale_after_s: float) -> bool:
    """Atomically take the right to run a job. False if another worker is running it."""
    cutoff = utcnow() - timedelta(seconds=stale_after_s)
    result = db.session.execute(
        update(ResearchJob)
        .where(
            ResearchJob.id == job_id,
            ResearchJob.status.in_(c.JOB_ACTIVE_STATUSES),
            or_(ResearchJob.heartbeat_at.is_(None), ResearchJob.heartbeat_at < cutoff),
        )
        .values(heartbeat_at=utcnow())
        .execution_options(synchronize_session=False)  # the database decides; no in-Python comparison
    )
    db.session.commit()
    db.session.expire_all()
    return result.rowcount == 1


def run_by_public_id(app, public_id: str) -> bool:
    """Entry point shared by every runner. Returns True if this call ran the job."""
    from agents.runner import default_context, run_job

    with app.app_context():
        try:
            job = db.session.scalar(select(ResearchJob).where(ResearchJob.public_id == public_id))
            if job is None or not job.is_active:
                return False
            if not claim(job.id, app.config["JOB_STALE_AFTER_S"]):
                logger.info("Job %s is already being run by another worker", public_id)
                return False
            run_job(default_context(app, job.id))
            return True
        except Exception:  # noqa: BLE001 - run_job handles its own errors; this is the last line
            logger.exception("Background job %s failed outside the workflow", public_id)
            return False
        finally:
            db.session.remove()


def enqueue(app, job: ResearchJob) -> str:
    """Start ``job`` with the configured runner. Returns the runner name used."""
    mode = app.config["JOB_RUNNER"]
    public_id = job.public_id
    if mode == "manual":
        return mode
    if mode == "celery":
        from jobs.celery_app import send

        task_id = send(app, public_id)
        job.celery_task_id = task_id
        db.session.commit()
        return mode
    if mode == "thread":
        _pool(app).submit(run_by_public_id, app, public_id)
        return mode
    raise ValueError(f"JOB_RUNNER must be thread, celery or manual, got {mode!r}")


def _pool(app) -> ThreadPoolExecutor:
    with _lock:
        pool = app.extensions.get("job_pool")
        if pool is None:
            pool = ThreadPoolExecutor(
                max_workers=max(1, app.config["JOB_WORKERS"]), thread_name_prefix="research-job"
            )
            app.extensions["job_pool"] = pool
        return pool


def stale_active_jobs(app) -> list[ResearchJob]:
    """Active jobs nobody is running: never claimed for a while, or heartbeat gone stale."""
    cutoff = utcnow() - timedelta(seconds=app.config["JOB_STALE_AFTER_S"])
    return list(
        db.session.scalars(
            select(ResearchJob).where(
                ResearchJob.status.in_(c.JOB_ACTIVE_STATUSES),
                or_(
                    ResearchJob.heartbeat_at < cutoff,
                    (ResearchJob.heartbeat_at.is_(None)) & (ResearchJob.created_at < cutoff),
                ),
            )
        )
    )


def init_app(app) -> None:
    from jobs.cli import register_cli

    register_cli(app)
