"""Authentication business logic (no Flask request/response handling here)."""

from __future__ import annotations

import enum
import logging
from datetime import timedelta

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from auth.models import (
    PURPOSE_RESET_PASSWORD,
    PURPOSE_VERIFY_EMAIL,
    AuthToken,
    User,
)
from auth.tokens import generate_token, hash_token
from core.audit import record_event
from core.db_utils import as_utc, utcnow
from core.mailer import send_email
from core.urls import external_url
from extensions import db

logger = logging.getLogger(__name__)

# Used to burn comparable CPU time when the email is unknown (timing-safe login).
_DUMMY_HASH = generate_password_hash("not-a-real-password")

# Minimum gap between emails of the same kind to one account.
EMAIL_COOLDOWN = timedelta(seconds=60)


class TokenStatus(enum.Enum):
    VALID = "valid"
    INVALID = "invalid"
    EXPIRED = "expired"
    USED = "used"


# -- Lookups -----------------------------------------------------------------
def get_user_by_email(email: str) -> User | None:
    return db.session.execute(select(User).where(User.email == email)).scalar_one_or_none()


def load_user_from_session_id(session_id: str) -> User | None:
    """Resolve a Flask-Login id of the form ``<id>:<auth_version>``."""
    try:
        raw_id, raw_version = session_id.split(":", 1)
        user = db.session.get(User, int(raw_id))
        if user is None or user.auth_version != int(raw_version):
            return None
    except (ValueError, AttributeError):
        return None
    return user


# -- Tokens ------------------------------------------------------------------
def issue_token(user: User, purpose: str, ttl: timedelta) -> str:
    """Create a fresh token, superseding any unused earlier ones. Returns the raw token."""
    now = utcnow()
    db.session.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user.id,
            AuthToken.purpose == purpose,
            AuthToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    raw, digest = generate_token()
    db.session.add(AuthToken(user_id=user.id, purpose=purpose, token_hash=digest, expires_at=now + ttl))
    db.session.commit()
    return raw


def lookup_token(raw: str | None, purpose: str) -> tuple[TokenStatus, AuthToken | None]:
    if not raw or len(raw) > 128:
        return TokenStatus.INVALID, None
    token = db.session.execute(
        select(AuthToken).where(AuthToken.token_hash == hash_token(raw), AuthToken.purpose == purpose)
    ).scalar_one_or_none()
    if token is None:
        return TokenStatus.INVALID, None
    if token.used_at is not None:
        return TokenStatus.USED, token
    if as_utc(token.expires_at) <= utcnow():
        return TokenStatus.EXPIRED, token
    return TokenStatus.VALID, token


def _consume(token: AuthToken) -> bool:
    """Atomically mark a token used. False if someone else consumed it first."""
    result = db.session.execute(
        update(AuthToken)
        .where(AuthToken.id == token.id, AuthToken.used_at.is_(None))
        .values(used_at=utcnow())
    )
    return result.rowcount == 1


def _recently_issued(user: User, purpose: str) -> bool:
    latest = db.session.execute(
        select(AuthToken.created_at)
        .where(AuthToken.user_id == user.id, AuthToken.purpose == purpose)
        .order_by(AuthToken.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return latest is not None and utcnow() - as_utc(latest) < EMAIL_COOLDOWN


# -- Registration & verification ---------------------------------------------
def register_user(email: str, password: str, display_name: str) -> User | None:
    """Create an account. Returns None if the email is already registered."""
    if get_user_by_email(email) is not None:
        return None
    user = User(email=email, display_name=display_name)
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.flush()
        record_event("user_registered", actor_user_id=user.id, target_type="user", target_id=user.id)
        db.session.commit()
    except IntegrityError:  # lost a race with a concurrent registration
        db.session.rollback()
        return None
    return user


def send_verification_email(user: User, *, background: bool = False) -> bool:
    raw = issue_token(user, PURPOSE_VERIFY_EMAIL, current_app.config["EMAIL_VERIFY_TTL"])
    link = external_url("auth.verify_email", token=raw)
    hours = int(current_app.config["EMAIL_VERIFY_TTL"].total_seconds() // 3600)
    return send_email(
        to=user.email,
        subject="Confirm your ResearchPilot AI email address",
        template="verify_email",
        background=background,
        display_name=user.display_name,
        link=link,
        expires_hours=hours,
    )


def resend_verification_email(user: User) -> str:
    """Return one of: 'already_verified', 'too_soon', 'sent', 'failed'."""
    if user.is_verified:
        return "already_verified"
    if _recently_issued(user, PURPOSE_VERIFY_EMAIL):
        return "too_soon"
    return "sent" if send_verification_email(user) else "failed"


def verify_email(raw_token: str) -> tuple[TokenStatus, User | None]:
    status, token = lookup_token(raw_token, PURPOSE_VERIFY_EMAIL)
    if status is not TokenStatus.VALID or token is None:
        return status, None
    if not _consume(token):
        db.session.rollback()
        return TokenStatus.USED, None
    user = db.session.get(User, token.user_id)
    if user is not None and user.email_verified_at is None:
        user.email_verified_at = utcnow()
        record_event("email_verified", actor_user_id=user.id)
    db.session.commit()
    return TokenStatus.VALID, user


# -- Login -------------------------------------------------------------------
def authenticate(email: str, password: str) -> User | None:
    user = get_user_by_email(email)
    if user is None:
        check_password_hash(_DUMMY_HASH, password)  # equalise timing
        return None
    if not user.check_password(password) or not user.is_enabled:
        return None
    user.last_login_at = utcnow()
    record_event("login_succeeded", actor_user_id=user.id)
    db.session.commit()
    return user


# -- Password reset & change -------------------------------------------------
def request_password_reset(email: str) -> None:
    """Send a reset link if the account exists. Silent otherwise (no enumeration)."""
    user = get_user_by_email(email)
    if user is None or not user.is_enabled:
        return
    if _recently_issued(user, PURPOSE_RESET_PASSWORD):
        return
    raw = issue_token(user, PURPOSE_RESET_PASSWORD, current_app.config["PASSWORD_RESET_TTL"])
    link = external_url("auth.reset_password", token=raw)
    minutes = int(current_app.config["PASSWORD_RESET_TTL"].total_seconds() // 60)
    send_email(
        to=user.email,
        subject="Reset your ResearchPilot AI password",
        template="reset_password",
        background=True,
        display_name=user.display_name,
        link=link,
        expires_minutes=minutes,
    )


def reset_password(raw_token: str, new_password: str) -> TokenStatus:
    status, token = lookup_token(raw_token, PURPOSE_RESET_PASSWORD)
    if status is not TokenStatus.VALID or token is None:
        return status
    if not _consume(token):
        db.session.rollback()
        return TokenStatus.USED
    user = db.session.get(User, token.user_id)
    if user is None:
        db.session.rollback()
        return TokenStatus.INVALID
    user.set_password(new_password)
    user.auth_version += 1  # signs out every existing session
    if user.email_verified_at is None:  # the link proved control of the mailbox
        user.email_verified_at = utcnow()
    record_event("password_reset", actor_user_id=user.id)
    db.session.commit()
    _notify_password_changed(user)
    return TokenStatus.VALID


def change_password(user: User, current_password: str, new_password: str) -> bool:
    if not user.check_password(current_password):
        return False
    user.set_password(new_password)
    user.auth_version += 1
    record_event("password_changed", actor_user_id=user.id)
    db.session.commit()
    _notify_password_changed(user)
    return True


def _notify_password_changed(user: User) -> None:
    send_email(
        to=user.email,
        subject="Your ResearchPilot AI password was changed",
        template="password_changed",
        background=True,
        display_name=user.display_name,
        reset_url=external_url("auth.forgot_password"),
    )
