"""Report models. A report belongs to a project (and so to its owner) and normally to the job
that produced it. Revisions keep the full edit history in PostgreSQL."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db_types import BigIntPK, JSONType, one_of, public_id
from core.db_utils import TimestampMixin, utcnow
from extensions import db
from research import constants as c


class ReportDocument(TimestampMixin, db.Model):
    __tablename__ = "report_documents"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_report_documents_job"),
        one_of("status", c.REPORT_STATUSES, "status_valid"),
        CheckConstraint("current_revision_no >= 0", name="revision_no_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, default=public_id, nullable=False)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("research_projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The report outlives its job record if the job is purged, so SET NULL rather than CASCADE.
    job_id: Mapped[int | None] = mapped_column(ForeignKey("research_jobs.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    citation_style: Mapped[str | None] = mapped_column(String(24))
    current_revision_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Optimistic locking: an editor saving with a stale version is rejected instead of overwriting.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    export_ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sections: Mapped[list[ReportSection]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="ReportSection.position"
    )
    revisions: Mapped[list[ReportRevision]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="ReportRevision.revision_no"
    )
    evaluations: Mapped[list[QualityEvaluation]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="QualityEvaluation.id"
    )


class ReportSection(TimestampMixin, db.Model):
    """A section or subsection. ``body`` is Markdown with ``[@S<source id>]`` citation markers."""

    __tablename__ = "report_sections"
    __table_args__ = (
        UniqueConstraint("report_id", "key", name="uq_report_sections_report_key"),
        CheckConstraint("level between 1 and 4", name="level_range"),
        Index("ix_report_sections_report_position", "report_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("report_documents.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("report_sections.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64), nullable=False)  # stable slug, e.g. executive_summary
    heading: Mapped[str] = mapped_column(String(300), nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    edited_by_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ReportRevision(db.Model):
    """History entry. ``kind='section'`` stores one section's text; ``kind='document'`` stores a
    whole-report snapshot in ``structure``. Regenerated text is stored as ``proposed`` and only
    becomes ``applied`` when the user accepts it; the user's own edits are never overwritten
    silently."""

    __tablename__ = "report_revisions"
    __table_args__ = (
        UniqueConstraint("report_id", "revision_no", name="uq_report_revisions_report_no"),
        one_of("kind", c.REVISION_KINDS, "kind_valid"),
        one_of("source", c.REVISION_SOURCES, "source_valid"),
        one_of("status", c.REVISION_STATUSES, "status_valid"),
        CheckConstraint("kind = 'document' or section_key is not null", name="section_has_key"),
        Index("ix_report_revisions_section", "section_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("report_documents.id", ondelete="CASCADE"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # SET NULL keeps the history of a deleted section; section_key still says which one it was.
    section_id: Mapped[int | None] = mapped_column(ForeignKey("report_sections.id", ondelete="SET NULL"))
    section_key: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="applied", nullable=False)
    heading: Mapped[str | None] = mapped_column(String(300))
    content: Mapped[str | None] = mapped_column(Text)
    structure: Mapped[dict | None] = mapped_column(JSONType)
    change_summary: Mapped[str | None] = mapped_column(String(500))
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    based_on_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("report_revisions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class QualityEvaluation(db.Model):
    """One score for one dimension in one evaluation run. Deterministic checks and LLM
    assessments are separate rows (``evaluator``) and must be shown as separate things: an LLM
    score is an opinion about quality, not proof of factual accuracy."""

    __tablename__ = "quality_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "report_id", "run_id", "evaluator", "dimension", name="uq_quality_evaluations_report_run_eval_dim"
        ),
        one_of("evaluator", c.EVALUATORS, "evaluator_valid"),
        one_of("dimension", c.QUALITY_DIMENSIONS, "dimension_valid"),
        one_of("status", c.EVALUATION_STATUSES, "status_valid"),
        CheckConstraint("score is null or (score >= 0 and score <= 100)", name="score_range"),
        CheckConstraint("status = 'scored' or score is null", name="unscored_has_no_score"),
        CheckConstraint("status <> 'scored' or score is not null", name="scored_has_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("report_documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)  # groups the rows of one evaluation
    revision_no: Mapped[int | None] = mapped_column(Integer)  # report revision that was evaluated
    evaluator: Mapped[str] = mapped_column(String(16), nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="scored", nullable=False)
    score: Mapped[float | None] = mapped_column(Float)  # 0-100; null when not assessable
    rubric_version: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str] = mapped_column(String(500), nullable=False)  # how the score was calculated
    passed_gate: Mapped[bool | None] = mapped_column(Boolean)
    details: Mapped[dict | None] = mapped_column(JSONType)  # inputs, warnings, affected claims/sources
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
