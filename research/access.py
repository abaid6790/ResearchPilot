"""Ownership-checked loaders. Use these (never ``db.session.get``) in every user-facing route.

A row that exists but belongs to someone else is reported exactly like a row that does not
exist (HTTP 404), so IDs cannot be probed. User-facing URLs use ``public_id`` (random, not
sequential), and these helpers still verify the owner on top of that.
"""

from __future__ import annotations

from flask import abort
from sqlalchemy import select

from extensions import db
from reports.models import QualityEvaluation, ReportDocument, ReportRevision, ReportSection
from research.models import (
    ClaimSource,
    EvidenceExcerpt,
    ResearchClaim,
    ResearchJob,
    ResearchProject,
    ResearchSource,
    ResearchTask,
)


def _one_or_404(statement):
    row = db.session.execute(statement).scalars().first()
    if row is None:
        abort(404)
    return row


def owned_project(user, public_id: str) -> ResearchProject:
    return _one_or_404(
        select(ResearchProject).where(
            ResearchProject.public_id == public_id, ResearchProject.user_id == user.id
        )
    )


def owned_job(user, public_id: str) -> ResearchJob:
    return _one_or_404(
        select(ResearchJob)
        .join(ResearchProject)
        .where(ResearchJob.public_id == public_id, ResearchProject.user_id == user.id)
    )


def owned_report(user, public_id: str) -> ReportDocument:
    return _one_or_404(
        select(ReportDocument)
        .join(ResearchProject)
        .where(ReportDocument.public_id == public_id, ResearchProject.user_id == user.id)
    )


def owned_task(user, task_id: int) -> ResearchTask:
    return _one_or_404(
        select(ResearchTask)
        .join(ResearchJob)
        .join(ResearchProject)
        .where(ResearchTask.id == task_id, ResearchProject.user_id == user.id)
    )


def owned_source(user, source_id: int) -> ResearchSource:
    return _one_or_404(
        select(ResearchSource)
        .join(ResearchJob)
        .join(ResearchProject)
        .where(ResearchSource.id == source_id, ResearchProject.user_id == user.id)
    )


def owned_excerpt(user, excerpt_id: int) -> EvidenceExcerpt:
    return _one_or_404(
        select(EvidenceExcerpt)
        .join(ResearchSource)
        .join(ResearchJob)
        .join(ResearchProject)
        .where(EvidenceExcerpt.id == excerpt_id, ResearchProject.user_id == user.id)
    )


def owned_claim(user, claim_id: int) -> ResearchClaim:
    return _one_or_404(
        select(ResearchClaim)
        .join(ResearchJob)
        .join(ResearchProject)
        .where(ResearchClaim.id == claim_id, ResearchProject.user_id == user.id)
    )


def owned_claim_link(user, link_id: int) -> ClaimSource:
    return _one_or_404(
        select(ClaimSource)
        .join(ResearchJob, ClaimSource.job_id == ResearchJob.id)
        .join(ResearchProject)
        .where(ClaimSource.id == link_id, ResearchProject.user_id == user.id)
    )


def owned_section(user, section_id: int) -> ReportSection:
    return _one_or_404(
        select(ReportSection)
        .join(ReportDocument)
        .join(ResearchProject)
        .where(ReportSection.id == section_id, ResearchProject.user_id == user.id)
    )


def owned_revision(user, revision_id: int) -> ReportRevision:
    return _one_or_404(
        select(ReportRevision)
        .join(ReportDocument)
        .join(ResearchProject)
        .where(ReportRevision.id == revision_id, ResearchProject.user_id == user.id)
    )


def owned_evaluation(user, evaluation_id: int) -> QualityEvaluation:
    return _one_or_404(
        select(QualityEvaluation)
        .join(ReportDocument)
        .join(ResearchProject)
        .where(QualityEvaluation.id == evaluation_id, ResearchProject.user_id == user.id)
    )


def projects_of(user):
    """Select statement for the user's own projects (for list pages)."""
    return select(ResearchProject).where(ResearchProject.user_id == user.id)
