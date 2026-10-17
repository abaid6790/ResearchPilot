"""Column types that behave the same on PostgreSQL (production) and SQLite (tests)."""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

#: JSONB on PostgreSQL, plain JSON elsewhere.
JSONType = JSON().with_variant(JSONB(), "postgresql")

#: BigInteger primary keys that still auto-increment on SQLite.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


def one_of(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    """CHECK constraint restricting a text column to a fixed vocabulary."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} in ({quoted})", name=name)


def public_id() -> str:
    """Unguessable identifier for URLs (32 hex chars from a random UUID)."""
    import uuid

    return uuid.uuid4().hex
