"""LLM provider layer (Gemini, Groq, OpenRouter) with key rotation and fallback.

Application code gets the shared router with ``get_router()`` and calls
``router.generate(LLMRequest...)``; nothing outside this package talks to a provider.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping

from providers.base import UsageRecord

logger = logging.getLogger(__name__)
_build_lock = threading.Lock()


def build_router(
    environ: Mapping[str, str], usage_recorder: Callable[[UsageRecord], None] | None = None, **overrides
):
    """Create an :class:`LLMRouter` from environment-style settings.

    ``overrides`` (clock, sleep, rng, health_store) exist for tests.
    """
    from providers.gemini import GeminiAdapter
    from providers.groq import GroqAdapter
    from providers.key_manager import KeyManager, MemoryHealthStore, RedisHealthStore
    from providers.models import ModelCatalog
    from providers.openrouter import OpenRouterAdapter
    from providers.router import LLMRouter, RouterConfig
    from providers.settings import ProviderSettings

    settings = ProviderSettings.from_env(environ)
    store = overrides.pop("health_store", None)
    if store is None:
        store = MemoryHealthStore()
        if settings.health_store == "redis":
            if settings.redis_url:
                store = RedisHealthStore(settings.redis_url)
            else:
                logger.warning("LLM_HEALTH_STORE=redis but REDIS_URL is empty; using in-memory key health")
    clock = overrides.get("clock")
    key_kwargs = {"clock": clock} if clock else {}
    keys = KeyManager(settings.keys, store, settings.key_manager, **key_kwargs)
    adapters = {
        "gemini": GeminiAdapter(settings.base_urls["gemini"]),
        "groq": GroqAdapter(settings.base_urls["groq"]),
        "openrouter": OpenRouterAdapter(settings.base_urls["openrouter"]),
    }
    config = RouterConfig(
        provider_order=settings.provider_order,
        max_total_attempts=settings.max_total_attempts,
        max_retries_per_key=settings.max_retries_per_key,
        max_validation_retries=settings.max_validation_retries,
        backoff_base_s=settings.backoff_base_s,
        backoff_cap_s=settings.backoff_cap_s,
        max_inline_wait_s=settings.max_inline_wait_s,
        request_timeout_s=settings.request_timeout_s,
        overall_deadline_s=settings.overall_deadline_s,
        max_concurrency=settings.max_concurrency,
    )
    router_kwargs = {k: v for k, v in overrides.items() if k in ("clock", "sleep", "rng")}
    return LLMRouter(
        adapters, keys, ModelCatalog.from_env(environ), config, usage_recorder=usage_recorder, **router_kwargs
    )


def get_router(app=None):
    """Return the process-wide router for the current Flask app (built on first use)."""
    from flask import current_app

    app = app or current_app._get_current_object()  # noqa: SLF001
    router = app.extensions.get("llm_router")
    if router is None:
        with _build_lock:
            router = app.extensions.get("llm_router")
            if router is None:
                environ = app.config.get("LLM_ENVIRON")
                if environ is None:
                    # Tests must never pick up real keys from a developer's environment.
                    environ = {} if app.config.get("TESTING") else os.environ
                recorder = None
                if app.config.get("LLM_RECORD_USAGE", not app.config.get("TESTING")):
                    from providers.usage import make_db_recorder

                    recorder = make_db_recorder(app)
                router = build_router(environ, usage_recorder=recorder)
                app.extensions["llm_router"] = router
    return router


def init_app(app) -> None:
    from providers.cli import register_cli
    from providers.routes import bp
    from providers.settings import SettingsError

    app.register_blueprint(bp)
    register_cli(app)
    if app.config.get("TESTING"):
        return
    try:
        router = get_router(app)
    except SettingsError as exc:
        raise RuntimeError(f"Invalid LLM provider settings: {exc}") from exc
    active = router.active_providers()
    if not active:
        logger.warning(
            "No LLM API keys are configured; research features that need a model will report an error "
            "until GEMINI_API_KEY_1 / GROQ_API_KEY_1 / OPENROUTER_API_KEY_1 is set."
        )
    else:
        counts = {p: len(router.keys.all_keys(p)) for p in active}
        logger.info(
            "LLM providers ready (fallback order): %s",
            ", ".join(f"{p}({n} key(s))" for p, n in counts.items()),
        )
