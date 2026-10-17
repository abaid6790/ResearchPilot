"""URL helpers.

Links placed in emails must be built from the configured ``APP_BASE_URL`` and never
from the incoming request's ``Host`` header. Otherwise an attacker could request a
password reset for a victim with a forged ``Host`` header and the victim would
receive a link pointing at the attacker's server ("password reset poisoning").
"""

from __future__ import annotations

from flask import current_app, url_for


def external_url(endpoint: str, **values) -> str:
    path = url_for(endpoint, _external=False, **values)
    return current_app.config["APP_BASE_URL"].rstrip("/") + path
