"""Persisting provider usage records (operational data for debugging and cost control).

Rows hold counts, timings, labels such as ``gemini#2`` and outcomes only: no prompts, no
responses, no keys. The recorder writes on its own connection, so a request that later rolls
back its own transaction does not lose the record of the API call it already spent.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, insert
from sqlalchemy.orm import Mapped, mapped_column

from core.db_types import BigIntPK
from core.db_utils import utcnow
from extensions import db
from providers.base import UsageRecord

logger = logging.getLogger(__name__)


class ProviderUsage(db.Model):
    __tablename__ = "provider_usage_records"
    __table_args__ = (
        Index("ix_provider_usage_user_created", "user_id", "created_at"),
        Index("ix_provider_usage_provider_created", "provider", "created_at"),
        Index("ix_provider_usage_job", "job_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # SET NULL: totals stay available for cost reporting after a user or job is deleted.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("research_jobs.id", ondelete="SET NULL"))
    request_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    key_label: Mapped[str | None] = mapped_column(String(24))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)  # "ok" or an ErrorKind value
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)


def make_db_recorder(app):
    """Return a ``usage_recorder`` callable for :class:`providers.router.LLMRouter`."""

    def record(rec: UsageRecord) -> None:
        values = {
            "created_at": utcnow(),
            "user_id": rec.user_id,
            "job_id": rec.job_id,
            "request_id": rec.request_id[:32],
            "task": rec.task[:32],
            "provider": rec.provider,
            "model": rec.model,
            "key_label": rec.key_label,
            "outcome": rec.outcome,
            "latency_ms": int(rec.latency_s * 1000),
            "prompt_tokens": rec.prompt_tokens,
            "completion_tokens": rec.completion_tokens,
            "http_status": rec.http_status,
        }
        try:
            with app.app_context(), db.engine.begin() as connection:
                connection.execute(insert(ProviderUsage), values)
        except Exception as exc:  # noqa: BLE001 - accounting must never break a request
            logger.warning("Could not save provider usage record (%s)", type(exc).__name__)

    return record
