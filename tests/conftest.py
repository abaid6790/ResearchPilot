"""Shared pytest fixtures.

Tests run against ``TEST_DATABASE_URL`` (PostgreSQL) when it is set and fall
back to in-memory SQLite otherwise. Email is captured, never sent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from auth.models import User  # noqa: E402
from core.db_utils import utcnow  # noqa: E402
from extensions import db  # noqa: E402

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def app():
    application = create_app("testing")

    @application.before_request
    def _isolate_request_globals():
        # The fixture keeps one app context open so tests can use db.session directly,
        # which makes Flask reuse it (and its ``g``) for every test-client request.
        # In production each request gets a fresh context, so reset ``g`` to match.
        from flask import g

        g.__dict__.clear()

    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def outbox(app):
    box = app.extensions.setdefault("mail_outbox", [])
    box.clear()
    return box


def get_csrf(client, path: str = "/auth/login") -> str:
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match, f"no CSRF token found on {path}"
    return match.group(1)


def post_form(client, path: str, data: dict, *, csrf_from: str | None = None, **kwargs):
    token = get_csrf(client, csrf_from or path)
    return client.post(path, data={**data, "csrf_token": token}, **kwargs)


def make_user(email="ada@example.com", password=PASSWORD, name="Ada", verified=True) -> User:
    user = User(email=email, display_name=name)
    user.set_password(password)
    if verified:
        user.email_verified_at = utcnow()
    db.session.add(user)
    db.session.commit()
    return user


def login(client, email="ada@example.com", password=PASSWORD, **kwargs):
    return post_form(client, "/auth/login", {"email": email, "password": password}, **kwargs)


def body_of(message) -> str:
    return message.get_body(preferencelist=("plain",)).get_content()


def link_in(message, kind: str) -> str:
    """Return the path (e.g. /auth/verify/<token>) of the link inside an email."""
    match = re.search(rf"https?://[^\s/]+(/auth/{kind}/[\w\-]+)", body_of(message))
    assert match, f"no {kind} link in email"
    return match.group(1)
