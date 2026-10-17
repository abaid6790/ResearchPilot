"""Provider status endpoint (no network calls, no secrets)."""

from __future__ import annotations

from flask import Blueprint, jsonify
from flask_login import login_required

from extensions import limiter
from providers import get_router

bp = Blueprint("llm_status", __name__)


def status_payload(router) -> dict:
    return {
        "fallback_order": router.config.provider_order,
        "active_providers": router.active_providers(),
        "models": {
            p: {tier: spec.id for tier, spec in router.catalog.models_for(p).items()} for p in router.adapters
        },
        "keys": router.keys.snapshot(),
        "metrics": router.metrics(),
    }


@bp.get("/health/providers")
@login_required
@limiter.limit("30 per minute")
def providers_status():
    """Key and provider health as the router currently knows it. Makes no provider calls,
    so it consumes no quota; use ``flask llm-ping`` for a live check."""
    return jsonify(status_payload(get_router()))
