"""Configuration validation and startup behaviour."""

from __future__ import annotations

import pytest

from app import create_app
from config import ConfigError, get_config_class, validate_config


def base(**overrides):
    cfg = {
        "ENV_NAME": "development",
        "TESTING": False,
        "SECRET_KEY": "x" * 40,
        "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg://u:p@localhost/db",
        "MAIL_SERVER": "smtp.example.com",
        "MAIL_DEFAULT_SENDER": "no-reply@example.com",
        "APP_BASE_URL": "https://example.com",
        "RATELIMIT_STORAGE_URI": "redis://localhost:6379/1",
    }
    cfg.update(overrides)
    return cfg


def test_valid_config_has_no_problems():
    assert validate_config(base()) == []
    assert validate_config(base(ENV_NAME="production")) == []


def test_missing_secret_and_database_are_reported_together():
    problems = validate_config(base(SECRET_KEY="", SQLALCHEMY_DATABASE_URI=""))
    joined = " ".join(problems)
    assert "SECRET_KEY" in joined and "DATABASE_URL" in joined
    assert len(problems) == 2


def test_short_secret_rejected():
    assert any("SECRET_KEY" in p for p in validate_config(base(SECRET_KEY="short")))


def test_non_postgres_url_rejected_outside_tests():
    assert any("PostgreSQL" in p for p in validate_config(base(SQLALCHEMY_DATABASE_URI="sqlite:///x.db")))
    assert validate_config(base(SQLALCHEMY_DATABASE_URI="sqlite:///x.db", TESTING=True)) == []


def test_production_requirements():
    problems = validate_config(
        base(
            ENV_NAME="production",
            MAIL_SERVER="",
            MAIL_DEFAULT_SENDER="",
            APP_BASE_URL="http://example.com",
            RATELIMIT_STORAGE_URI="memory://",
        )
    )
    joined = " ".join(problems)
    for expected in ("MAIL_SERVER", "MAIL_DEFAULT_SENDER", "https://", "RATELIMIT_STORAGE_URI"):
        assert expected in joined


def test_tls_and_ssl_are_mutually_exclusive():
    assert any("MAIL_USE_TLS" in p for p in validate_config(base(MAIL_USE_TLS=True, MAIL_USE_SSL=True)))


def test_unknown_environment_name():
    with pytest.raises(ConfigError, match="Unknown FLASK_ENV"):
        get_config_class("staging")


def test_create_app_fails_fast_with_helpful_message():
    with pytest.raises(ConfigError) as excinfo:
        create_app("testing", {"SECRET_KEY": ""})
    assert "SECRET_KEY" in str(excinfo.value)
    assert ".env.example" in str(excinfo.value)
