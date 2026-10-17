"""Application audit events: who did what to which record, without personal content.

Audit rows never store passwords, tokens, API keys, email addresses, IP addresses or report
text. ``actor_user_id`` becomes NULL if the account is deleted, so the trail of *what happened*
survives while the link to the person does not (see docs/DATABASE_SCHEMA.md for retention).
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db_types import BigIntPK, JSONType
from core.db_utils import utcnow
from core.logging_utils import redact
from extensions import db

EVENT_TYPES = frozenset({
    "user_registered", "email_verified", "login_succeeded", "password_changed", "password_reset",
    "project_created", "project_deleted", "job_started", "job_cancelled", "job_finished",
    "report_edited", "report_restored", "report_exported",
    "account_deletion_requested", "data_export_requested",
})  # fmt: skip

_FORBIDDEN_KEYS = ("password", "token", "secret", "api_key", "email", "authorization", "cookie")


class AuditEvent(db.Model):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))  # internal id or public_id
    details: Mapped[dict | None] = mapped_column(JSONType)


def record_event(
    event_type: str,
    *,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    target_id: object = None,
    **details: object,
) -> AuditEvent:
    """Add an audit event to the current session. The caller commits, so the event is saved
    atomically with the action it describes (and discarded if that action rolls back)."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown audit event type {event_type!r}")
    clean = {}
    for key, value in details.items():
        if any(bad in key.lower() for bad in _FORBIDDEN_KEYS):
            raise ValueError(f"audit detail {key!r} could hold sensitive data and is not allowed")
        clean[key] = json.loads(redact(json.dumps(value, default=str)))
    event = AuditEvent(
        event_type=event_type,
        actor_user_id=actor_user_id,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id)[:64],
        details=clean or None,
    )
    db.session.add(event)
    return event
