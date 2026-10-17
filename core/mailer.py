"""SMTP email delivery with honest success / failure reporting.

Uses ``smtplib`` directly so every connection has a hard timeout. Messages are
rendered in the request thread; delivery can optionally happen in a background
thread so that response time does not reveal whether an address exists.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage

from flask import Flask, current_app, render_template

logger = logging.getLogger(__name__)


def mail_is_configured(app: Flask | None = None) -> bool:
    cfg = (app or current_app).config
    return bool(cfg.get("MAIL_SERVER") and cfg.get("MAIL_DEFAULT_SENDER"))


def _build_message(cfg, to: str, subject: str, text_body: str, html_body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg["MAIL_DEFAULT_SENDER"]
    message["To"] = to
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def _deliver(app: Flask, message: EmailMessage, template: str) -> bool:
    cfg = app.config
    # Test mode: capture instead of sending, so tests never touch the network.
    if cfg.get("MAIL_SUPPRESS_SEND"):
        app.extensions.setdefault("mail_outbox", []).append(message)
        return True
    timeout = cfg.get("MAIL_TIMEOUT_SECONDS", 15)
    try:
        if cfg.get("MAIL_USE_SSL"):
            server = smtplib.SMTP_SSL(
                cfg["MAIL_SERVER"],
                cfg["MAIL_PORT"],
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=timeout)
        with server:
            if cfg.get("MAIL_USE_TLS") and not cfg.get("MAIL_USE_SSL"):
                server.starttls(context=ssl.create_default_context())
            if cfg.get("MAIL_USERNAME") and cfg.get("MAIL_PASSWORD"):
                server.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            server.send_message(message)
    except Exception as exc:  # noqa: BLE001 - smtplib/socket raise many types
        logger.error("Email delivery failed (%s) for template %s", type(exc).__name__, template)
        return False
    logger.info("Email delivered using template %s", template)
    return True


def send_email(*, to: str, subject: str, template: str, background: bool = False, **context) -> bool:
    """Render ``email/<template>.{txt,html}`` and send it.

    Returns True when the SMTP server accepted the message. With
    ``background=True`` delivery happens in a thread and the return value only
    says whether the message was queued (SMTP is configured); failures are
    logged.
    """
    app = current_app._get_current_object()  # noqa: SLF001 - needed for the thread
    if not mail_is_configured(app):
        logger.error("Email not sent: SMTP is not configured (MAIL_SERVER / MAIL_DEFAULT_SENDER).")
        return False

    context.setdefault("app_name", "ResearchPilot AI")
    message = _build_message(
        app.config,
        to,
        subject,
        render_template(f"email/{template}.txt", **context),
        render_template(f"email/{template}.html", **context),
    )

    if background and not app.config.get("MAIL_SUPPRESS_SEND"):
        threading.Thread(
            target=_deliver, args=(app, message, template), daemon=True, name="mail-sender"
        ).start()
        return True
    return _deliver(app, message, template)
