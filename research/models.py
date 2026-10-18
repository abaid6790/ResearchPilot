"""Research models: projects, jobs, job events, tasks, sources, evidence, claims.

Ownership: ``research_projects.user_id`` is the only ownership column. Every other table reaches
its owner through ``project -> user``; use ``research.access`` to load rows so that a user can
never reach another user's data by guessing an ID.

Deletion rules (all enforced by foreign keys): deleting a user deletes their projects and,
through ``ON DELETE CASCADE``, everything below them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db_types import BigIntPK, JSONType, one_of, public_id
from core.db_utils import TimestampMixin, utcnow
from extensions import db
from research import constants as c


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class ResearchProject(TimestampMixin, db.Model):
    """What the user asked for (the request and its settings). Runs are ``ResearchJob`` rows."""

    __tablename__ = "research_projects"
    __table_args__ = (
        one_of("depth", c.DEPTHS, "depth_valid"),
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        Index("ix_research_projects_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, default=public_id, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    objectives: Mapped[str | None] = mapped_column(Text)
    depth: Mapped[str] = mapped_column(String(16), default="standard", nullable=False)
    # Validated by research.schemas.ResearchSettings before it is stored.
    settings: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    uploads: Mapped[list[ResearchUpload]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="ResearchUpload.id"
    )
    jobs: Mapped[list[ResearchJob]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ResearchJob.id",
    )

    def __repr__(self) -> str:
        return f"<ResearchProject id={self.id}>"


class ResearchJob(TimestampMixin, db.Model):
    """One execution of the workflow for a project."""

    __tablename__ = "research_jobs"
    __table_args__ = (
        one_of("status", c.JOB_STATUSES, "status_valid"),
        CheckConstraint("research_round >= 0 and revision_round >= 0", name="rounds_non_negative"),
        # At most one active job per project, enforced by the database (not just by application code).
        Index(
            "uq_research_jobs_one_active_per_project",
            "project_id",
            unique=True,
            postgresql_where=text(f"status in ({_in_list(c.JOB_ACTIVE_STATUSES)})"),
            sqlite_where=text(f"status in ({_in_list(c.JOB_ACTIVE_STATUSES)})"),
        ),
        Index("ix_research_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, default=public_id, nullable=False)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("research_projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default=c.JOB_QUEUED, nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(64))
    current_agent: Mapped[str | None] = mapped_column(String(32))
    research_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revision_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Settings as they were when the job started, so later edits to the project do not change a run.
    config_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    # Workflow checkpoint (typed state) so an interrupted job can resume. Never contains secrets.
    checkpoint: Mapped[dict | None] = mapped_column(JSONType)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    # Refreshed while a worker runs the job; a stale value means the worker died (see jobs/dispatch.py).
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when quality gates were not met and the user must decide whether to continue.
    awaiting_decision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unresolved_issues: Mapped[list | None] = mapped_column(JSONType)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))  # user-safe, redacted
    event_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # last event number
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[ResearchProject] = relationship(back_populates="jobs")
    events: Mapped[list[ResearchJobEvent]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="ResearchJobEvent.seq"
    )
    tasks: Mapped[list[ResearchTask]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="ResearchTask.position"
    )
    sources: Mapped[list[ResearchSource]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="ResearchSource.rank"
    )

    @property
    def is_active(self) -> bool:
        return self.status in c.JOB_ACTIVE_STATUSES

    @property
    def is_finished(self) -> bool:
        return self.status in c.JOB_FINISHED_STATUSES


class ResearchJobEvent(db.Model):
    """Append-only progress log. Feeds the live progress view; ``seq`` lets a reconnecting client
    ask for everything after the last event it saw."""

    __tablename__ = "research_job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_research_job_events_job_seq"),
        one_of("level", c.EVENT_LEVELS, "level_valid"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)  # e.g. stage_changed, query_run
    level: Mapped[str] = mapped_column(String(8), default="info", nullable=False)
    stage: Mapped[str | None] = mapped_column(String(64))
    agent: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    data: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ResearchTask(TimestampMixin, db.Model):
    __tablename__ = "research_tasks"
    __table_args__ = (
        one_of("kind", c.TASK_KINDS, "kind_valid"),
        one_of("status", c.TASK_STATUSES, "status_valid"),
        one_of("agent", c.AGENTS, "agent_valid"),
        Index("ix_research_tasks_job_status", "job_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("research_tasks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    agent: Mapped[str] = mapped_column(String(16), nullable=False)
    specialist_type: Mapped[str | None] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(300), nullable=False)  # the question or subtopic
    description: Mapped[str | None] = mapped_column(Text)
    queries: Mapped[list | None] = mapped_column(JSONType)  # search queries actually used
    evidence_needed: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parallel_group: Mapped[int | None] = mapped_column(Integer)  # tasks sharing a group may run together
    round: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchSource(TimestampMixin, db.Model):
    """A source collected for a job. Citations in the report point at these rows."""

    __tablename__ = "research_sources"
    __table_args__ = (
        UniqueConstraint("job_id", "url_hash", name="uq_research_sources_job_url"),
        UniqueConstraint("id", "job_id", name="uq_research_sources_id_job"),  # target of composite FKs
        one_of("origin", c.SOURCE_ORIGINS, "origin_valid"),
        one_of("fetch_status", c.FETCH_STATUSES, "fetch_status_valid"),
        one_of("link_status", c.LINK_STATUSES, "link_status_valid"),
        one_of("metadata_status", c.METADATA_STATUSES, "metadata_status_valid"),
        one_of("category", c.SOURCE_CATEGORIES, "category_valid"),
        CheckConstraint(
            "relevance_score is null or (relevance_score >= 0 and relevance_score <= 1)",
            name="relevance_range",
        ),
        CheckConstraint(
            "quality_score is null or (quality_score >= 0 and quality_score <= 1)", name="quality_range"
        ),
        CheckConstraint("url is not null or upload_name is not null", name="has_locator"),
        Index("ix_research_sources_job_domain", "job_id", "domain"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("research_tasks.id", ondelete="SET NULL"))
    origin: Mapped[str] = mapped_column(String(16), default="web_search", nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048))  # as returned by the search provider
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    url_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 of the normalised URL (dedupe key)
    upload_name: Mapped[str | None] = mapped_column(String(255))  # file name for uploaded documents
    domain: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # only if the source states it
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(String(32), default="other", nullable=False)
    fetch_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    link_status: Mapped[str] = mapped_column(String(16), default="unchecked", nullable=False)
    metadata_status: Mapped[str] = mapped_column(String(16), default="unchecked", nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 of extracted text
    relevance_score: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warnings: Mapped[list | None] = mapped_column(JSONType)  # e.g. ["paywalled", "metadata mismatch"]

    excerpts: Mapped[list[EvidenceExcerpt]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="EvidenceExcerpt.id"
    )


class EvidenceExcerpt(db.Model):
    """A passage taken from a source. Stored verbatim; ``is_verbatim`` is False for paraphrases
    so a paraphrase is never presented as a quotation."""

    __tablename__ = "evidence_excerpts"
    __table_args__ = (
        UniqueConstraint("source_id", "content_hash", name="uq_evidence_excerpts_source_hash"),
        CheckConstraint("length(text) > 0", name="text_not_empty"),
        CheckConstraint(
            "relevance_score is null or (relevance_score >= 0 and relevance_score <= 1)",
            name="relevance_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("research_sources.id", ondelete="CASCADE"), index=True, nullable=False
    )
    task_id: Mapped[int | None] = mapped_column(ForeignKey("research_tasks.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_verbatim: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    locator: Mapped[str | None] = mapped_column(String(200))  # page number, section heading, offsets
    relevance_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ResearchClaim(TimestampMixin, db.Model):
    __tablename__ = "research_claims"
    __table_args__ = (
        UniqueConstraint("id", "job_id", name="uq_research_claims_id_job"),  # target of composite FKs
        one_of("claim_type", c.CLAIM_TYPES, "claim_type_valid"),
        one_of("support_status", c.CLAIM_SUPPORT, "support_status_valid"),
        CheckConstraint(
            "confidence is null or (confidence >= 0 and confidence <= 1)", name="confidence_range"
        ),
        Index("ix_research_claims_job_status", "job_id", "support_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("research_tasks.id", ondelete="SET NULL"))
    report_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("report_sections.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(12), default="fact", nullable=False)
    support_status: Mapped[str] = mapped_column(String(16), default="unassessed", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)  # the analyst's confidence, not a proof
    notes: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    links: Mapped[list[ClaimSource]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        back_populates="claim",
        primaryjoin="and_(ClaimSource.claim_id == ResearchClaim.id, ClaimSource.job_id == ResearchClaim.job_id)",
        foreign_keys="[ClaimSource.claim_id, ClaimSource.job_id]",
    )


class ClaimSource(db.Model):
    """Which source supports, contradicts or gives context for a claim.

    ``job_id`` plus the composite foreign keys guarantee, in the database, that a claim and the
    source it cites belong to the same job (so a claim can never cite another user's source).
    """

    __tablename__ = "claim_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id", "job_id"], ["research_claims.id", "research_claims.job_id"],
            ondelete="CASCADE", name="fk_claim_sources_claim_job",
        ),
        ForeignKeyConstraint(
            ["source_id", "job_id"], ["research_sources.id", "research_sources.job_id"],
            ondelete="CASCADE", name="fk_claim_sources_source_job",
        ),
        UniqueConstraint("claim_id", "source_id", "relation", name="uq_claim_sources_claim_source_relation"),
        one_of("relation", c.CLAIM_RELATIONS, "relation_valid"),
        one_of("support_level", c.SUPPORT_LEVELS, "support_level_valid"),
        Index("ix_claim_sources_source", "source_id"),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False)
    excerpt_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_excerpts.id", ondelete="SET NULL"))
    relation: Mapped[str] = mapped_column(String(12), default="supports", nullable=False)
    # Whether the passage really backs the claim. "unreviewed" until the validator or user checks it.
    support_level: Mapped[str] = mapped_column(String(12), default="unreviewed", nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    claim: Mapped[ResearchClaim] = relationship(
        back_populates="links",
        primaryjoin="and_(ClaimSource.claim_id == ResearchClaim.id, ClaimSource.job_id == ResearchClaim.job_id)",
        foreign_keys=[claim_id, job_id],
    )
    source: Mapped[ResearchSource] = relationship(
        primaryjoin="and_(ClaimSource.source_id == ResearchSource.id, ClaimSource.job_id == ResearchSource.job_id)",
        foreign_keys=[source_id, job_id],
        viewonly=True,
    )


class ResearchUpload(db.Model):
    """A document the user attached to a project. Only the extracted text is kept: the original
    file is never written to disk, so there is nothing to clean up or serve back."""

    __tablename__ = "research_uploads"
    __table_args__ = (
        UniqueConstraint("project_id", "sha256", name="uq_research_uploads_project_sha"),
        CheckConstraint("size_bytes >= 0", name="size_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("research_projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)  # display name only
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # pdf, docx, txt
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    warnings: Mapped[list | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
