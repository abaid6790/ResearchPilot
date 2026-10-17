"""Authentication flows: registration, verification, login, reset, change password."""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select, update

from auth import services
from auth.models import PURPOSE_RESET_PASSWORD, PURPOSE_VERIFY_EMAIL, AuthToken, User
from auth.services import TokenStatus
from core.db_utils import utcnow
from extensions import db
from tests.conftest import (
    PASSWORD,
    body_of,
    get_csrf,
    link_in,
    login,
    make_user,
    post_form,
)

NEW_PASSWORD = "an entirely different passphrase 42"


def count_users() -> int:
    return db.session.execute(select(func.count()).select_from(User)).scalar_one()


def register(client, email="grace@example.com", password=PASSWORD, name="Grace", **kwargs):
    return post_form(
        client,
        "/auth/register",
        {"display_name": name, "email": email, "password": password, "confirm_password": password},
        **kwargs,
    )


# --------------------------------------------------------------------- register
def test_register_creates_unverified_user_and_sends_link(client, outbox):
    response = register(client, follow_redirects=True)
    assert response.status_code == 200
    assert "If that email address can be registered" in response.get_data(as_text=True)

    user = db.session.execute(select(User).where(User.email == "grace@example.com")).scalar_one()
    assert user.email_verified_at is None
    assert user.password_hash != PASSWORD and user.password_hash.startswith(("scrypt:", "pbkdf2:"))
    assert len(outbox) == 1
    assert outbox[0]["To"] == "grace@example.com"
    assert link_in(outbox[0], "verify").startswith("/auth/verify/")


def test_register_normalises_email_case_and_whitespace(client, outbox):
    register(client, email="  Grace@Example.COM ")
    assert db.session.execute(select(User.email)).scalar_one() == "grace@example.com"


def test_register_duplicate_email_is_indistinguishable(client, outbox):
    make_user("grace@example.com")
    response = register(client, follow_redirects=True)
    assert response.status_code == 200
    assert "If that email address can be registered" in response.get_data(as_text=True)
    assert count_users() == 1
    assert outbox == []  # nothing sent, nothing revealed


def test_register_rejects_bad_input(client, outbox):
    cases = [
        {"password": "short1", "confirm_password": "short1"},
        {"password": "1234567890", "confirm_password": "1234567890"},
        {"password": PASSWORD, "confirm_password": PASSWORD + "x"},
        {"email": "not-an-email"},
        {"display_name": "   "},
    ]
    for override in cases:
        data = {
            "display_name": "Grace",
            "email": "grace@example.com",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            **override,
        }
        response = post_form(client, "/auth/register", data)
        assert response.status_code == 200, override
        assert 'class="field__errors"' in response.get_data(as_text=True), override
    assert count_users() == 0
    assert outbox == []


def test_register_rejects_password_equal_to_email(client):
    data = {
        "display_name": "G",
        "email": "grace-hopper-1906@example.com",
        "password": "grace-hopper-1906@example.com",
        "confirm_password": "grace-hopper-1906@example.com",
    }
    response = post_form(client, "/auth/register", data)
    assert "same as your email" in response.get_data(as_text=True)
    assert count_users() == 0


# ----------------------------------------------------------------- verification
def test_verification_link_requires_post_and_is_single_use(client, outbox):
    register(client)
    path = link_in(outbox[0], "verify")

    # A GET (what mail scanners do) must not consume the token.
    page = client.get(path)
    assert page.status_code == 200
    assert "Confirm email address" in page.get_data(as_text=True)
    user = db.session.execute(select(User)).scalar_one()
    assert user.email_verified_at is None

    confirmed = post_form(client, path, {}, csrf_from=path, follow_redirects=True)
    assert confirmed.status_code == 200
    assert "Email confirmed" in confirmed.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.execute(select(User)).scalar_one().email_verified_at is not None

    again = client.get(path)
    assert again.status_code == 400
    assert "already used" in again.get_data(as_text=True)


def test_expired_verification_token(client, outbox):
    register(client)
    path = link_in(outbox[0], "verify")
    db.session.execute(update(AuthToken).values(expires_at=utcnow() - timedelta(minutes=1)))
    db.session.commit()
    response = client.get(path)
    assert response.status_code == 400
    assert "expired" in response.get_data(as_text=True)
    status, user = services.verify_email(path.rsplit("/", 1)[1])
    assert status is TokenStatus.EXPIRED and user is None


def test_invalid_verification_token(client):
    response = client.get("/auth/verify/this-is-not-a-real-token")
    assert response.status_code == 400
    assert "not valid" in response.get_data(as_text=True)


def test_tokens_are_stored_hashed(client, outbox):
    register(client)
    raw = link_in(outbox[0], "verify").rsplit("/", 1)[1]
    stored = db.session.execute(select(AuthToken)).scalar_one()
    assert stored.token_hash != raw
    assert len(stored.token_hash) == 64
    assert stored.purpose == PURPOSE_VERIFY_EMAIL


def test_token_consumption_is_atomic(app):
    user = make_user(verified=False)
    raw = services.issue_token(user, PURPOSE_VERIFY_EMAIL, timedelta(hours=1))
    _, token = services.lookup_token(raw, PURPOSE_VERIFY_EMAIL)
    assert services._consume(token) is True
    assert services._consume(token) is False  # second consumer loses


def test_new_token_supersedes_old_one(app):
    user = make_user(verified=False)
    first = services.issue_token(user, PURPOSE_VERIFY_EMAIL, timedelta(hours=1))
    second = services.issue_token(user, PURPOSE_VERIFY_EMAIL, timedelta(hours=1))
    assert services.lookup_token(first, PURPOSE_VERIFY_EMAIL)[0] is TokenStatus.USED
    assert services.lookup_token(second, PURPOSE_VERIFY_EMAIL)[0] is TokenStatus.VALID


def test_token_purposes_do_not_cross(app):
    user = make_user()
    raw = services.issue_token(user, PURPOSE_RESET_PASSWORD, timedelta(hours=1))
    assert services.lookup_token(raw, PURPOSE_VERIFY_EMAIL)[0] is TokenStatus.INVALID


def test_resend_verification_with_cooldown(client, outbox):
    make_user(verified=False)
    login(client)
    token = get_csrf(client, "/auth/verify-pending")

    first = client.post("/auth/resend-verification", data={"csrf_token": token}, follow_redirects=True)
    assert "We sent a new confirmation link" in first.get_data(as_text=True)
    assert len(outbox) == 1

    token = get_csrf(client, "/auth/verify-pending")
    second = client.post("/auth/resend-verification", data={"csrf_token": token}, follow_redirects=True)
    assert "moment ago" in second.get_data(as_text=True)
    assert len(outbox) == 1


# ------------------------------------------------------------------------ login
def test_login_success_and_logout(client):
    make_user()
    response = login(client)
    assert response.status_code == 302 and response.headers["Location"].endswith("/dashboard")
    assert client.get("/dashboard").status_code == 200

    token = get_csrf(client, "/dashboard")
    out = client.post("/auth/logout", data={"csrf_token": token})
    assert out.status_code == 302
    assert client.get("/dashboard").status_code == 302  # back to login


def test_logout_rejects_get(client):
    make_user()
    login(client)
    assert client.get("/auth/logout").status_code == 405


def test_login_failure_is_generic(client):
    make_user()
    wrong_password = login(client, password="wrong password here")
    unknown_user = login(client, email="nobody@example.com")
    for response in (wrong_password, unknown_user):
        assert response.status_code == 401
        assert "Incorrect email address or password." in response.get_data(as_text=True)


def test_unverified_user_is_held_at_verify_pending(client):
    make_user(verified=False)
    response = login(client)
    assert response.headers["Location"].endswith("/auth/verify-pending")
    blocked = client.get("/dashboard")
    assert blocked.status_code == 302 and "/auth/verify-pending" in blocked.headers["Location"]
    assert client.get("/settings/profile").status_code == 302


def test_disabled_account_cannot_sign_in(client):
    user = make_user()
    user.is_enabled = False
    db.session.commit()
    assert login(client).status_code == 401


def test_dashboard_requires_login(client):
    response = client.get("/dashboard")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]
    assert "next=" in response.headers["Location"]


def test_login_next_redirect_is_same_site_only(client):
    make_user()
    ok = login(client, query_string={"next": "/settings/profile"})
    assert ok.headers["Location"].endswith("/settings/profile")

    for evil in ["https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)"]:
        other = client.application.test_client()
        response = login(other, query_string={"next": evil})
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/dashboard"), evil


def test_malformed_session_id_is_rejected(app):
    assert services.load_user_from_session_id("garbage") is None
    assert services.load_user_from_session_id("999:1") is None
    assert services.load_user_from_session_id("") is None


# --------------------------------------------------------------- password reset
def test_forgot_password_does_not_reveal_accounts(client, outbox):
    make_user()
    known = post_form(client, "/auth/forgot-password", {"email": "ada@example.com"}, follow_redirects=True)
    unknown = post_form(
        client, "/auth/forgot-password", {"email": "nobody@example.com"}, follow_redirects=True
    )
    assert known.status_code == unknown.status_code == 200
    message = "If an account exists for that address"
    assert message in known.get_data(as_text=True) and message in unknown.get_data(as_text=True)
    assert len(outbox) == 1 and outbox[0]["To"] == "ada@example.com"


def test_full_password_reset_flow(client, outbox):
    make_user()
    post_form(client, "/auth/forgot-password", {"email": "ada@example.com"})
    path = link_in(outbox[0], "reset")
    assert client.get(path).status_code == 200

    mismatch = post_form(client, path, {"password": NEW_PASSWORD, "confirm_password": "x" * 12})
    assert mismatch.status_code == 200 and 'class="field__errors"' in mismatch.get_data(as_text=True)

    done = post_form(
        client, path, {"password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}, follow_redirects=True
    )
    assert "Your password has been updated" in done.get_data(as_text=True)

    other = client.application.test_client()
    assert login(other, password=PASSWORD).status_code == 401
    assert login(other, password=NEW_PASSWORD).status_code == 302
    # A confirmation notice goes to the account owner too.
    assert any("password was changed" in m["Subject"] for m in outbox)


def test_reset_token_cannot_be_reused(client, outbox):
    make_user()
    post_form(client, "/auth/forgot-password", {"email": "ada@example.com"})
    path = link_in(outbox[0], "reset")
    post_form(client, path, {"password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    again = client.get(path)
    assert again.status_code == 400 and "already used" in again.get_data(as_text=True)
    blocked = post_form(client, "/auth/forgot-password", {"email": "x@example.com"})  # fresh csrf source
    assert blocked.status_code in (200, 302)
    assert services.reset_password(path.rsplit("/", 1)[1], "yet another passphrase 99") is TokenStatus.USED


def test_expired_reset_token(client, outbox):
    make_user()
    post_form(client, "/auth/forgot-password", {"email": "ada@example.com"})
    path = link_in(outbox[0], "reset")
    db.session.execute(update(AuthToken).values(expires_at=utcnow() - timedelta(seconds=5)))
    db.session.commit()
    response = client.get(path)
    assert response.status_code == 400 and "expired" in response.get_data(as_text=True)


def test_reset_request_cooldown_and_supersede(client, outbox):
    make_user()
    post_form(client, "/auth/forgot-password", {"email": "ada@example.com"})
    post_form(client, "/auth/forgot-password", {"email": "ada@example.com"})
    assert len(outbox) == 1  # second request inside the cooldown is silently skipped


def test_password_reset_signs_out_other_sessions(client, outbox):
    make_user()
    login(client)
    assert client.get("/dashboard").status_code == 200

    attacker_view = client.application.test_client()
    post_form(attacker_view, "/auth/forgot-password", {"email": "ada@example.com"})
    path = link_in(outbox[-1], "reset")
    post_form(attacker_view, path, {"password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})

    assert client.get("/dashboard").status_code == 302  # old session no longer valid


def test_reset_marks_email_verified(client, outbox):
    make_user(verified=False)
    post_form(client, "/auth/forgot-password", {"email": "ada@example.com"})
    path = link_in(outbox[0], "reset")
    post_form(client, path, {"password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    db.session.expire_all()
    assert db.session.execute(select(User)).scalar_one().is_verified


# -------------------------------------------------------------- change password
def test_change_password(client, outbox):
    make_user()
    login(client)
    other_device = client.application.test_client()
    login(other_device)

    token = get_csrf(client, "/settings/profile")
    wrong = client.post(
        "/settings/password",
        data={
            "csrf_token": token,
            "current_password": "nope nope nope",
            "password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )
    assert wrong.status_code == 400
    assert "not your current password" in wrong.get_data(as_text=True)

    token = get_csrf(client, "/settings/profile")
    ok = client.post(
        "/settings/password",
        data={
            "csrf_token": token,
            "current_password": PASSWORD,
            "password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=True,
    )
    assert "Password changed" in ok.get_data(as_text=True)
    assert client.get("/dashboard").status_code == 200  # this device stays signed in
    assert other_device.get("/dashboard").status_code == 302  # others are signed out
    assert any("password was changed" in m["Subject"] for m in outbox)


def test_update_profile_name(client):
    make_user()
    login(client)
    token = get_csrf(client, "/settings/profile")
    response = client.post(
        "/settings/profile",
        data={"csrf_token": token, "display_name": "  Ada Lovelace "},
        follow_redirects=True,
    )
    assert "Profile updated" in response.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.execute(select(User.display_name)).scalar_one() == "Ada Lovelace"


def test_templates_escape_user_content(client):
    make_user(name="<script>alert(1)</script>")
    login(client)
    html = client.get("/dashboard").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_email_links_ignore_a_forged_host_header(client, outbox):
    """Password-reset poisoning: links must come from APP_BASE_URL, not the Host header."""
    make_user()
    forged = {"Host": "evil.example", "X-Forwarded-Host": "evil.example"}
    page = client.get("/auth/forgot-password", headers=forged).get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    response = client.post(
        "/auth/forgot-password",
        data={"email": "ada@example.com", "csrf_token": token},
        headers=forged,
    )
    assert response.status_code == 302
    assert len(outbox) == 1
    body = body_of(outbox[0])
    assert "evil.example" not in body
    assert f"{client.application.config['APP_BASE_URL']}/auth/reset/" in body
