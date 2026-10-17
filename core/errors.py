"""Consistent HTML / JSON error handling."""

from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

from extensions import db

logger = logging.getLogger(__name__)

MESSAGES = {
    400: ("Bad request", "The request could not be understood or was missing required data."),
    401: ("Sign in required", "Please sign in to continue."),
    403: ("Access denied", "You do not have permission to view this page."),
    404: ("Page not found", "We could not find what you were looking for."),
    405: ("Method not allowed", "That action is not supported for this address."),
    413: ("Upload too large", "The data you sent is larger than the allowed limit."),
    429: ("Too many requests", "You are doing that too often. Please wait a moment and try again."),
    500: ("Something went wrong", "An unexpected error occurred. It has been logged."),
}


def wants_json() -> bool:
    if request.path.startswith("/api/") or request.path.startswith("/health"):
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    accepts = request.accept_mimetypes
    return best == "application/json" and accepts[best] > accepts["text/html"]


def _render(status: int, description: str | None = None):
    title, default_message = MESSAGES.get(status, ("Error", "Something went wrong."))
    message = description or default_message
    if wants_json():
        return jsonify({"error": {"status": status, "title": title, "message": message}}), status
    template = f"errors/{status}.html"
    try:
        return render_template(template, title=title, message=message), status
    except Exception:  # noqa: BLE001 - fall back for statuses without a template
        return render_template("errors/500.html", title=title, message=message), status


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(CSRFError)
    def handle_csrf(error: CSRFError):
        return _render(400, "Your form session expired or was invalid. Reload the page and try again.")

    @app.errorhandler(HTTPException)
    def handle_http(error: HTTPException):
        status = error.code or 500
        if status < 400:  # redirects (e.g. trailing-slash 308) are not errors
            return error
        # Only show our own safe wording for client-facing statuses.
        return _render(status)

    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception):
        logger.exception("Unhandled exception on %s %s", request.method, request.path)
        db.session.rollback()
        return _render(500)
