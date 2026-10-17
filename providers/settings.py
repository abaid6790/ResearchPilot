"""Provider settings read from environment variables.

API keys are held in :class:`KeyInfo` objects (whose ``repr`` hides the secret) and are never
copied into ``app.config``, so they cannot leak through config dumps or debug pages.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from providers.key_manager import KeyInfo, KeyManagerConfig

logger = logging.getLogger(__name__)

PROVIDERS = ("gemini", "groq", "openrouter")
MAX_KEYS_PER_PROVIDER = 8
PLACEHOLDERS = {"", "changeme", "your-key-here", "your_key_here", "xxx"}

DEFAULT_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


class SettingsError(ValueError):
    pass


def _float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise SettingsError(f"{name} must not be negative")
    return value


def _int(environ: Mapping[str, str], name: str, default: int) -> int:
    return int(_float(environ, name, default))


@dataclass
class ProviderSettings:
    provider_order: list[str]
    keys: list[KeyInfo] = field(repr=False)  # hides secrets from repr/logging
    base_urls: dict[str, str]
    key_manager: KeyManagerConfig
    request_timeout_s: float = 60.0
    overall_deadline_s: float = 180.0
    max_total_attempts: int = 8
    max_retries_per_key: int = 2
    max_validation_retries: int = 1
    max_concurrency: int = 8
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    max_inline_wait_s: float = 5.0
    health_store: str = "memory"  # "memory" or "redis"
    redis_url: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> ProviderSettings:
        order_raw = (environ.get("LLM_PROVIDER_ORDER") or "gemini,groq,openrouter").lower()
        order = [p.strip() for p in order_raw.split(",") if p.strip()]
        unknown = [p for p in order if p not in PROVIDERS]
        if unknown or not order:
            raise SettingsError(
                f"LLM_PROVIDER_ORDER has unknown provider(s) {unknown or order_raw!r}; "
                f"valid values: {', '.join(PROVIDERS)}"
            )
        order = list(dict.fromkeys(order))  # drop duplicates, keep order

        keys: list[KeyInfo] = []
        seen_secrets: set[str] = set()
        for provider in PROVIDERS:
            for index in range(1, MAX_KEYS_PER_PROVIDER + 1):
                name = f"{provider.upper()}_API_KEY_{index}"
                secret = (environ.get(name) or "").strip()
                if secret.lower() in PLACEHOLDERS:
                    continue
                if secret in seen_secrets:
                    logger.warning("%s duplicates another configured key and was ignored", name)
                    continue
                seen_secrets.add(secret)
                # Gemini quotas are per Google Cloud project: keys from the same project share
                # them. Declare that with GEMINI_API_KEY_n_PROJECT=<any label>; by default each
                # key is assumed to belong to its own project.
                group = (environ.get(f"{name}_PROJECT") or "").strip() or f"{provider}-key{index}"
                keys.append(KeyInfo(provider, f"{provider}#{index}", secret, group))

        base_urls = {
            p: ((environ.get(f"{p.upper()}_BASE_URL") or "").strip() or DEFAULT_BASE_URLS[p]).rstrip("/")
            for p in PROVIDERS
        }
        health = (environ.get("LLM_HEALTH_STORE") or "memory").strip().lower()
        if health not in ("memory", "redis"):
            raise SettingsError("LLM_HEALTH_STORE must be 'memory' or 'redis'")

        return cls(
            provider_order=order,
            keys=keys,
            base_urls=base_urls,
            key_manager=KeyManagerConfig(
                rate_limit_cooldown_s=_float(environ, "LLM_RATE_LIMIT_COOLDOWN_S", 30.0),
                auth_cooldown_s=_float(environ, "LLM_AUTH_COOLDOWN_S", 21600.0),
            ),
            request_timeout_s=_float(environ, "LLM_REQUEST_TIMEOUT_S", 60.0),
            overall_deadline_s=_float(environ, "LLM_OVERALL_DEADLINE_S", 180.0),
            max_total_attempts=_int(environ, "LLM_MAX_TOTAL_ATTEMPTS", 8),
            max_retries_per_key=_int(environ, "LLM_MAX_RETRIES_PER_KEY", 2),
            max_validation_retries=_int(environ, "LLM_MAX_VALIDATION_RETRIES", 1),
            max_concurrency=max(1, _int(environ, "LLM_MAX_CONCURRENCY", 8)),
            backoff_base_s=_float(environ, "LLM_BACKOFF_BASE_S", 0.5),
            backoff_cap_s=_float(environ, "LLM_BACKOFF_CAP_S", 8.0),
            max_inline_wait_s=_float(environ, "LLM_MAX_INLINE_WAIT_S", 5.0),
            health_store=health,
            redis_url=(environ.get("REDIS_URL") or "").strip(),
        )

    def key_counts(self) -> dict[str, int]:
        return {p: sum(1 for k in self.keys if k.provider == p) for p in PROVIDERS}
