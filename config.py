"""Centralised configuration for ResearchPilot AI.

All settings come from environment variables (optionally loaded from a local
``.env`` file). Nothing secret is hard-coded here. ``validate_config`` runs at
startup and reports every problem at once with a fix hint.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str = "") -> str:
    """Read a variable; an empty value (as in .env.example) counts as unset."""
    value = os.getenv(name, "").strip()
    return value if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


class BaseConfig:
    ENV_NAME = "base"
    DEBUG = False
    TESTING = False

    SECRET_KEY = _env("SECRET_KEY")  # noqa: S105 - read from the environment
    APP_BASE_URL = _env("APP_BASE_URL", "http://localhost:5000").rstrip("/")

    # Database
    SQLALCHEMY_DATABASE_URI = _env("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Sessions and cookies
    SESSION_COOKIE_NAME = "rp_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_env_int("SESSION_LIFETIME_HOURS", 12))
    REMEMBER_COOKIE_DURATION = timedelta(0)

    # CSRF (Flask-WTF)
    WTF_CSRF_TIME_LIMIT = 60 * 60 * _env_int("SESSION_LIFETIME_HOURS", 12)

    # Account deletion: days between the request and the removal (the user can cancel meanwhile)
    ACCOUNT_DELETION_GRACE_DAYS = _env_int("ACCOUNT_DELETION_GRACE_DAYS", 7)

    # Request limits
    MAX_CONTENT_LENGTH = _env_int("MAX_CONTENT_LENGTH_MB", 16) * 1024 * 1024

    # Email (Flask-Mail)
    MAIL_SERVER = _env("MAIL_SERVER")
    MAIL_PORT = _env_int("MAIL_PORT", 587)
    MAIL_USE_TLS = _env_bool("MAIL_USE_TLS", True)
    MAIL_USE_SSL = _env_bool("MAIL_USE_SSL", False)
    MAIL_USERNAME = _env("MAIL_USERNAME") or None
    MAIL_PASSWORD = _env("MAIL_PASSWORD") or None
    MAIL_DEFAULT_SENDER = _env("MAIL_DEFAULT_SENDER") or None
    MAIL_TIMEOUT_SECONDS = _env_int("MAIL_TIMEOUT_SECONDS", 15)

    # Auth tuning
    EMAIL_VERIFY_TTL = timedelta(hours=_env_int("EMAIL_VERIFY_TTL_HOURS", 24))
    PASSWORD_RESET_TTL = timedelta(minutes=_env_int("PASSWORD_RESET_TTL_MINUTES", 60))
    PASSWORD_MIN_LENGTH = 10
    PASSWORD_MAX_LENGTH = 128

    # Rate limiting. Use Redis (e.g. REDIS_URL) in production so limits are
    # shared across Gunicorn workers; memory:// only works per process.
    RATELIMIT_STORAGE_URI = _env("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_ENABLED = True

    # Reverse proxy: number of trusted proxies in front of the app (0 = none).
    TRUSTED_PROXY_COUNT = _env_int("TRUSTED_PROXY_COUNT", 0)

    REDIS_URL = _env("REDIS_URL")

    # Background research jobs. JOB_RUNNER: "thread" (in this process; fine for one user or development,
    # jobs stop if the process restarts), "celery" (separate worker; needs Redis) or "manual" (nothing
    # runs by itself: used by tests and `flask run-research`).
    JOB_RUNNER = _env("JOB_RUNNER", "thread").lower()
    JOB_WORKERS = _env_int("JOB_WORKERS", 2)
    JOB_STALE_AFTER_S = _env_int("JOB_STALE_AFTER_S", 300)
    CELERY_BROKER_URL = _env("CELERY_BROKER_URL") or _env("REDIS_URL")
    MAX_ACTIVE_JOBS_PER_USER = _env_int("MAX_ACTIVE_JOBS_PER_USER", 2)
    JOB_SSE_MAX_SECONDS = _env_int("JOB_SSE_MAX_SECONDS", 60)


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = _env_bool("FLASK_DEBUG", False)


class TestingConfig(BaseConfig):
    ENV_NAME = "testing"
    TESTING = True
    SECRET_KEY = "testing-secret-key-not-for-production-use-0123456789"  # noqa: S105
    SQLALCHEMY_DATABASE_URI = _env("TEST_DATABASE_URL", "sqlite:///:memory:")
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    WTF_CSRF_ENABLED = True
    RATELIMIT_ENABLED = False
    MAIL_SUPPRESS_SEND = True  # tests capture messages in app.extensions["mail_outbox"]
    MAIL_SERVER = "localhost"
    MAIL_DEFAULT_SENDER = "ResearchPilot <no-reply@example.test>"
    APP_BASE_URL = "http://localhost"
    JOB_RUNNER = "manual"
    JOB_SSE_MAX_SECONDS = 3


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"
    SESSION_COOKIE_SECURE = True


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config_class(name: str | None = None) -> type[BaseConfig]:
    name = (name or _env("FLASK_ENV", "development")).lower()
    try:
        return CONFIG_MAP[name]
    except KeyError as exc:
        valid = ", ".join(sorted(CONFIG_MAP))
        raise ConfigError(f"Unknown FLASK_ENV {name!r}. Valid values: {valid}") from exc


def validate_config(cfg: type[BaseConfig] | dict) -> list[str]:
    """Return a list of human-readable problems (empty list means valid)."""

    def get(key: str, default=None):
        return cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)

    problems: list[str] = []
    env_name = get("ENV_NAME", "development")
    testing = bool(get("TESTING", False))

    secret = get("SECRET_KEY") or ""
    if len(secret) < 32:
        problems.append(
            "SECRET_KEY is missing or shorter than 32 characters. Generate one with: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )

    db_uri = get("SQLALCHEMY_DATABASE_URI") or ""
    if not db_uri:
        problems.append(
            "DATABASE_URL is not set. Example: "
            "postgresql+psycopg://researchpilot:PASSWORD@localhost:5432/researchpilot"
        )
    elif not testing and not db_uri.startswith("postgresql"):
        problems.append("DATABASE_URL must be a PostgreSQL URL (postgresql+psycopg://...).")

    if env_name == "production" and not testing:
        if not get("MAIL_SERVER"):
            problems.append("MAIL_SERVER is required in production (email verification is mandatory).")
        if not get("MAIL_DEFAULT_SENDER"):
            problems.append("MAIL_DEFAULT_SENDER is required in production.")
        if not str(get("APP_BASE_URL", "")).startswith("https://"):
            problems.append("APP_BASE_URL must use https:// in production.")
        if str(get("RATELIMIT_STORAGE_URI", "memory://")).startswith("memory"):
            problems.append(
                "RATELIMIT_STORAGE_URI should point to Redis in production "
                "(e.g. redis://localhost:6379/1) so limits are shared across workers."
            )

    if get("MAIL_USE_TLS") and get("MAIL_USE_SSL"):
        problems.append("MAIL_USE_TLS and MAIL_USE_SSL cannot both be true.")

    return problems


def warnings_for(cfg: type[BaseConfig] | dict) -> list[str]:
    """Non-fatal configuration notes shown in the startup log."""

    def get(key: str, default=None):
        return cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)

    notes: list[str] = []
    if not get("MAIL_SERVER"):
        notes.append(
            "MAIL_SERVER is not set: verification and password-reset emails cannot be sent. "
            "For local development run Mailpit and set MAIL_SERVER=localhost "
            "MAIL_PORT=1025 MAIL_USE_TLS=false."
        )
    if not get("MAIL_DEFAULT_SENDER"):
        notes.append("MAIL_DEFAULT_SENDER is not set.")
    return notes
