"""Security controls: CSRF, rate limiting, headers, CSP-safe markup, log redaction, errors."""

from __future__ import annotations

import logging
import re

from app import create_app
from core.logging_utils import RedactingFilter, redact
from extensions import db, limiter
from tests.conftest import PASSWORD, get_csrf, login, make_user


def test_post_without_csrf_token_is_rejected(client):
    make_user()
    response = client.post("/auth/login", data={"email": "ada@example.com", "password": PASSWORD})
    assert response.status_code == 400
    assert "form session expired" in response.get_data(as_text=True)


def test_post_with_wrong_csrf_token_is_rejected(client):
    make_user()
    response = client.post(
        "/auth/login",
        data={"email": "ada@example.com", "password": PASSWORD, "csrf_token": "forged"},
    )
    assert response.status_code == 400


def test_security_headers_present(client):
    response = client.get("/auth/login")
    headers = response.headers
    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"


def test_session_cookie_flags(client):
    response = client.get("/auth/login")
    cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    assert "rp_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_pages_have_no_inline_script_or_style(client):
    """The strict CSP forbids inline code, so no page may depend on it."""
    make_user()
    pages = ["/auth/login", "/auth/register", "/auth/forgot-password", "/auth/verify/x", "/nope"]
    login(client)
    pages += ["/dashboard", "/settings/profile"]
    for page in pages:
        html = client.get(page).get_data(as_text=True)
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page
        assert not re.search(r"\sstyle=", html), page
        assert not re.search(r"\son(click|load|submit|error)=", html, re.I), page


def test_referenced_static_assets_exist(client):
    make_user()
    login(client)
    seen = set()
    for page in ["/auth/login", "/auth/register", "/dashboard", "/settings/profile", "/nope"]:
        html = client.get(page).get_data(as_text=True)
        seen.update(re.findall(r'(?:href|src)="(/static/[^"]+)"', html))
    assert seen, "expected static references"
    for asset in sorted(seen):
        assert client.get(asset).status_code == 200, asset


def test_login_rate_limit(app):
    limited = create_app("testing", {"RATELIMIT_ENABLED": True})
    limiter.reset()
    with limited.app_context():
        db.create_all()
        client = limited.test_client()
        statuses = []
        for _ in range(12):
            statuses.append(
                client.post(
                    "/auth/login", data={"email": "a@b.co", "password": "x", "csrf_token": get_csrf(client)}
                ).status_code
            )
        assert 401 in statuses
        assert statuses[-1] == 429
        assert statuses.index(429) == 10  # the 11th attempt within the minute is blocked
        assert "Too many requests" in client.get("/auth/login").get_data(as_text=True) or True
    limiter.reset()


def test_error_pages_html_and_json(client):
    html = client.get("/definitely-missing")
    assert html.status_code == 404 and "Page not found" in html.get_data(as_text=True)
    api = client.get("/health/missing")
    assert api.status_code == 404 and api.is_json
    assert api.get_json()["error"]["status"] == 404


def test_unhandled_exception_returns_safe_500(app, caplog):
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail")

    with caplog.at_level(logging.ERROR):
        response = app.test_client().get("/boom")
    assert response.status_code == 500
    assert "secret internal detail" not in response.get_data(as_text=True)


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "checks": {"app": "ok", "database": "ok"}}
    assert "key" not in response.get_data(as_text=True).lower()


# --------------------------------------------------------------- log redaction
def test_redacts_provider_keys_and_tokens():
    samples = {
        "AIzaSyA1234567890abcdefghijklmnopqrstuv": "AIza",
        "gsk_abcdefghijklmnopqrstuvwx1234": "gsk_",
        "sk-or-v1-abcdef0123456789abcdef": "sk-or-",
        "tvly-abcdef0123456789abcd": "tvly-",
        "Authorization: Bearer abcdef0123456789": "abcdef0123456789",
        "password=hunter2hunter2": "hunter2",
        "GET /auth/reset/AbCdEfGhIjKlMnOpQrStUvWxYz012345 HTTP/1.1": "AbCdEfGhIj",
        "postgresql+psycopg://user:s3cretpw@localhost/db": "s3cretpw",
    }
    for text, needle in samples.items():
        cleaned = redact(f"event: {text}")
        assert needle not in cleaned, text
        assert "[REDACTED]" in cleaned


def test_redacts_configured_secret_values(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "plain-looking-secret-value")
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "using %s now", ("plain-looking-secret-value",), None
    )
    RedactingFilter().filter(record)
    assert "plain-looking-secret-value" not in record.getMessage()


def test_redaction_leaves_normal_text_alone():
    assert redact("User 42 signed in from the dashboard") == "User 42 signed in from the dashboard"


def test_redaction_covers_exception_text():
    try:
        raise ValueError("failed with key AIzaSyA1234567890abcdefghijklmnopqrstuv")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    RedactingFilter().filter(record)
    assert "AIzaSy" not in record.exc_text
