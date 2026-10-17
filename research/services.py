"""Transactional operations on research data. Callers get ownership-checked objects first."""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.audit import record_event
from core.db_utils import utcnow
from core.logging_utils import redact
from extensions import db
from research import constants as c
from research.models import ResearchJob, ResearchJobEvent, ResearchProject
from research.schemas import ResearchSettings


class ActiveJobExistsError(Exception):
    """The project already has a queued or running job."""


def create_project(
    user, *, title: str, topic: str, objectives: str | None = None, settings: Mapping | None = None
):
    """Validate settings and create a project. Raises ``pydantic.ValidationError`` on bad input."""
    validated = ResearchSettings.model_validate(dict(settings or {}))
    project = ResearchProject(
        user_id=user.id,
        title=title.strip(),
        topic=topic.strip(),
        objectives=(objectives or "").strip() or None,
        depth=validated.depth,
        settings=validated.model_dump(mode="json"),
    )
    db.session.add(project)
    db.session.flush()
    record_event("project_created", actor_user_id=user.id, target_type="project", target_id=project.public_id)
    db.session.commit()
    return project


def create_job(project: ResearchProject) -> ResearchJob:
    """Queue a new job. The database allows only one active job per project; a second attempt
    (even from a concurrent request) raises :class:`ActiveJobExistsError`."""
    job = ResearchJob(project_id=project.id, config_snapshot=dict(project.settings))
    db.session.add(job)
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise ActiveJobExistsError from exc
    append_event(job, "job_queued", "Research job queued.", stage="queued", agent="system")
    record_event("job_started", actor_user_id=project.user_id, target_type="job", target_id=job.public_id)
    db.session.commit()
    return job


def append_event(
    job: ResearchJob, event_type: str, message: str, *, level: str = "info", stage: str | None = None,
    agent: str | None = None, data: dict | None = None,
) -> ResearchJobEvent:  # fmt: skip
    """Add a progress event with the next sequence number (atomic, safe under concurrency).

    Message and data are passed through the secret redactor; never put prompts, provider
    responses or credentials in them.
    """
    seq = db.session.execute(
        update(ResearchJob)
        .where(ResearchJob.id == job.id)
        .values(event_seq=ResearchJob.event_seq + 1)
        .returning(ResearchJob.event_seq)
    ).scalar_one()
    safe_data = json.loads(redact(json.dumps(data, default=str))) if data else None
    event = ResearchJobEvent(
        job_id=job.id, seq=seq, event_type=event_type, level=level, stage=stage, agent=agent,
        message=redact(message)[:500], data=safe_data,
    )  # fmt: skip
    db.session.add(event)
    db.session.flush()
    db.session.refresh(job, attribute_names=["event_seq"])
    return event


def events_after(job: ResearchJob, last_seq: int = 0, limit: int = 200) -> list[ResearchJobEvent]:
    """Events a reconnecting client has not seen yet."""
    return list(
        db.session.execute(
            select(ResearchJobEvent)
            .where(ResearchJobEvent.job_id == job.id, ResearchJobEvent.seq > last_seq)
            .order_by(ResearchJobEvent.seq)
            .limit(limit)
        ).scalars()
    )


def request_cancel(job: ResearchJob, actor_user_id: int) -> bool:
    """Ask a running job to stop. Returns False if it had already finished. Atomic: a job that
    finishes at the same moment is not overwritten."""
    result = db.session.execute(
        update(ResearchJob)
        .where(ResearchJob.id == job.id, ResearchJob.status.in_(c.JOB_ACTIVE_STATUSES))
        .values(cancel_requested_at=utcnow())
    )
    if result.rowcount == 0:
        db.session.rollback()
        return False
    db.session.refresh(job)
    append_event(
        job, "cancel_requested", "Cancellation requested by the user.", level="warning", agent="system"
    )
    record_event("job_cancelled", actor_user_id=actor_user_id, target_type="job", target_id=job.public_id)
    db.session.commit()
    return True


def attach_documents(project, documents) -> int:
    """Save extracted text of validated uploads on a project. Duplicates (same hash) are skipped."""
    from research.models import ResearchUpload

    existing = {u.sha256 for u in project.uploads}
    added = 0
    for doc in documents:
        if doc.sha256 in existing:
            continue
        db.session.add(
            ResearchUpload(
                project_id=project.id, filename=doc.filename, kind=doc.kind, size_bytes=doc.size,
                sha256=doc.sha256, page_count=doc.page_count, text=doc.text, warnings=doc.warnings or None,
            )
        )  # fmt: skip
        existing.add(doc.sha256)
        added += 1
    if added:
        db.session.commit()
    return added
