"""Basic application health endpoint.

``/health`` reports only on this application and its database. External
provider availability (LLM / search APIs) is deliberately a separate concern
and will get its own endpoint when the provider layer exists.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify
from sqlalchemy import text

from extensions import db, limiter

logger = logging.getLogger(__name__)
bp = Blueprint("health", __name__)


@bp.get("/health")
@limiter.exempt
def health():
    checks = {"app": "ok"}
    try:
        db.session.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.error("Health check: database unavailable (%s)", type(exc).__name__)
        checks["database"] = "unavailable"
    healthy = all(v == "ok" for v in checks.values())
    return jsonify(status="ok" if healthy else "degraded", checks=checks), (200 if healthy else 503)
