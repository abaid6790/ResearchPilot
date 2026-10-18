"""Celery wiring. The web app only *sends* tasks; the worker (``jobs/worker.py``) runs them."""

from __future__ import annotations

from celery import Celery

TASK_NAME = "research.run_job"


def _configure(celery: Celery, app) -> Celery:
    url = app.config.get("CELERY_BROKER_URL")
    if not url:
        raise RuntimeError("JOB_RUNNER=celery needs CELERY_BROKER_URL or REDIS_URL")
    celery.conf.update(
        broker_url=url,
        result_backend=None,  # results live in the database, not in Redis
        task_ignore_result=True,
        task_acks_late=True,  # a job taken by a worker that dies is delivered again
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,  # one long job at a time per worker process
        broker_connection_retry_on_startup=True,
        broker_transport_options={"visibility_timeout": 6 * 3600},
        task_serializer="json",
        accept_content=["json"],
    )
    return celery


def send(app, public_id: str) -> str:
    """Queue a job from the web process."""
    client = app.extensions.get("celery_client")
    if client is None:
        client = _configure(Celery("researchpilot"), app)
        app.extensions["celery_client"] = client
    return client.send_task(TASK_NAME, args=[public_id]).id


def make_worker_app(app) -> Celery:
    """The Celery application a worker process runs (see jobs/worker.py)."""
    celery = _configure(Celery("researchpilot"), app)

    @celery.task(name=TASK_NAME, bind=True, max_retries=0)
    def run_research_job(self, public_id: str) -> bool:
        from jobs import run_by_public_id

        return run_by_public_id(app, public_id)

    return celery
