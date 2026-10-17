"""Email delivery over a real SMTP conversation (local aiosmtpd server, no external network)."""

from __future__ import annotations

import email
import socket
import time
from email import policy

import pytest
from aiosmtpd.controller import Controller

from core.mailer import mail_is_configured, send_email
from tests.conftest import PASSWORD, post_form


class Collector:
    def __init__(self):
        self.envelopes = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 - aiosmtpd API
        self.envelopes.append(envelope)
        return "250 Message accepted"

    def messages(self):
        return [email.message_from_bytes(e.content, policy=policy.default) for e in self.envelopes]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def smtp(app):
    collector = Collector()
    port = free_port()
    controller = Controller(collector, hostname="127.0.0.1", port=port)
    controller.start()
    app.config.update(
        MAIL_SUPPRESS_SEND=False,
        MAIL_SERVER="127.0.0.1",
        MAIL_PORT=port,
        MAIL_USE_TLS=False,
        MAIL_USE_SSL=False,
        MAIL_TIMEOUT_SECONDS=3,
    )
    yield collector
    controller.stop()


def wait_for(collector, count=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while len(collector.envelopes) < count and time.monotonic() < deadline:
        time.sleep(0.05)
    return collector.messages()


def test_message_is_delivered_via_smtp(app, smtp):
    with app.test_request_context():
        ok = send_email(
            to="ada@example.com",
            subject="Confirm",
            template="verify_email",
            display_name="Ada",
            link="http://localhost/auth/verify/tok123",
            expires_hours=24,
        )
    assert ok is True
    (message,) = smtp.messages()
    assert message["To"] == "ada@example.com"
    assert message["From"] == app.config["MAIL_DEFAULT_SENDER"]
    assert message["Subject"] == "Confirm"
    text = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "http://localhost/auth/verify/tok123" in text
    assert 'href="http://localhost/auth/verify/tok123"' in html


def test_html_email_escapes_user_supplied_name(app, smtp):
    with app.test_request_context():
        send_email(
            to="x@example.com",
            subject="s",
            template="verify_email",
            display_name="<b>bold</b>",
            link="http://localhost/x",
            expires_hours=1,
        )
    html = smtp.messages()[0].get_body(preferencelist=("html",)).get_content()
    assert "<b>bold</b>" not in html and "&lt;b&gt;bold&lt;/b&gt;" in html


def test_unreachable_server_reports_failure_without_raising(app, caplog):
    app.config.update(
        MAIL_SUPPRESS_SEND=False,
        MAIL_SERVER="127.0.0.1",
        MAIL_PORT=free_port(),
        MAIL_USE_TLS=False,
        MAIL_TIMEOUT_SECONDS=2,
    )
    with app.test_request_context():
        ok = send_email(
            to="ada@example.com",
            subject="s",
            template="verify_email",
            display_name="Ada",
            link="http://localhost/auth/verify/SECRETTOKEN0123456789",
            expires_hours=1,
        )
    assert ok is False
    assert "Email delivery failed" in caplog.text
    assert "SECRETTOKEN" not in caplog.text


def test_unconfigured_smtp_reports_failure(app):
    app.config.update(MAIL_SERVER="", MAIL_SUPPRESS_SEND=False)
    with app.test_request_context():
        assert mail_is_configured() is False
        assert (
            send_email(
                to="a@b.co",
                subject="s",
                template="verify_email",
                display_name="A",
                link="http://x",
                expires_hours=1,
            )
            is False
        )


def test_background_delivery(app, smtp):
    with app.test_request_context():
        queued = send_email(
            to="ada@example.com",
            subject="bg",
            template="reset_password",
            background=True,
            display_name="Ada",
            link="http://localhost/auth/reset/abc",
            expires_minutes=60,
        )
    assert queued is True
    assert len(wait_for(smtp)) == 1


def test_registration_sends_a_real_email_end_to_end(app, smtp):
    client = app.test_client()
    post_form(
        client,
        "/auth/register",
        {
            "display_name": "Grace",
            "email": "grace@example.com",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    (message,) = wait_for(smtp)
    assert message["To"] == "grace@example.com"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "/auth/verify/" in body
