"""Authentication models: users and one-time email tokens."""

from __future__ import annotations

from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from core.db_utils import TimestampMixin, utcnow
from extensions import db

PURPOSE_VERIFY_EMAIL = "verify_email"
PURPOSE_RESET_PASSWORD = "reset_password"  # noqa: S105 - a label, not a credential


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Bumped on password change/reset; sessions carrying an older value are rejected.
    auth_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- Flask-Login integration -------------------------------------------
    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.is_enabled

    def get_id(self) -> str:
        return f"{self.id}:{self.auth_version}"

    # -- Helpers -----------------------------------------------------------
    @property
    def is_verified(self) -> bool:
        return self.email_verified_at is not None

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:  # never include the email or hash in logs
        return f"<User id={self.id}>"


class AuthToken(db.Model):
    """Single-use token for email verification or password reset.

    Only the SHA-256 digest of the token is stored. The raw token exists only in
    the emailed link.
    """

    __tablename__ = "auth_tokens"
    __table_args__ = (
        CheckConstraint(
            f"purpose in ('{PURPOSE_VERIFY_EMAIL}', '{PURPOSE_RESET_PASSWORD}')",
            name="purpose_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<AuthToken id={self.id} purpose={self.purpose}>"
