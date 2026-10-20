"""ResearchPilot AI application factory.

Run locally:      flask --app app run --debug
Production WSGI:  gunicorn wsgi:app
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

from config import BASE_DIR, ConfigError, get_config_class, validate_config, warnings_for
from core.errors import register_error_handlers, wants_json
from core.logging_utils import configure_logging
from core.security import register_security_headers
from extensions import csrf, db, limiter, login_manager, migrate

logger = logging.getLogger(__name__)


def create_app(config_name: str | None = None, overrides: dict | None = None) -> Flask:
    configure_logging()
    app = Flask(__name__, instance_path=str(BASE_DIR / "instance"))
    app.config.from_object(get_config_class(config_name))
    if overrides:
        app.config.update(overrides)

    problems = validate_config(app.config)
    if problems:
        details = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(
            f"ResearchPilot AI cannot start because of configuration problems:\n{details}\n"
            "Copy .env.example to .env and fill in the missing values."
        )
    for note in warnings_for(app.config):
        logger.warning(note)

    if app.config["TRUSTED_PROXY_COUNT"] > 0:
        n = app.config["TRUSTED_PROXY_COUNT"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)  # type: ignore[method-assign]

    _init_extensions(app)
    _register_blueprints(app)
    from providers import init_app as init_llm
    from research_tools import init_app as init_tools

    init_llm(app)
    init_tools(app)
    register_error_handlers(app)
    register_security_headers(app)
    return app


def _init_extensions(app: Flask) -> None:
    _enable_sqlite_foreign_keys()
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)

    @limiter.request_filter
    def skip_static() -> bool:
        return request.endpoint == "static"

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"

    @login_manager.user_loader
    def load_user(session_id: str):
        from auth.services import load_user_from_session_id

        return load_user_from_session_id(session_id)

    @login_manager.unauthorized_handler
    def unauthorized():
        from flask import redirect, url_for

        if wants_json():
            return jsonify(
                error={"status": 401, "title": "Sign in required", "message": "Please sign in to continue."}
            ), 401
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


def _enable_sqlite_foreign_keys() -> None:
    """SQLite ignores foreign keys unless asked; PostgreSQL always enforces them."""
    import sqlite3

    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    if getattr(_enable_sqlite_foreign_keys, "done", False):
        return

    @event.listens_for(Engine, "connect")
    def _pragma(dbapi_connection, _record):
        if isinstance(dbapi_connection, sqlite3.Connection):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _enable_sqlite_foreign_keys.done = True  # type: ignore[attr-defined]


def _register_blueprints(app: Flask) -> None:
    # Importing models here makes them visible to Flask-Migrate / Alembic.
    from auth import models  # noqa: F401
    from auth.routes import bp as auth_bp
    from core import audit  # noqa: F401
    from core.health import bp as health_bp
    from dashboard.routes import bp as dashboard_bp
    from providers import usage  # noqa: F401
    from reports import models as report_models  # noqa: F401
    from reports.routes import bp as editor_bp
    from research import models as research_models  # noqa: F401
    from research.routes import bp as research_bp
    from settings.cli import register_cli as register_settings_cli
    from settings.routes import bp as settings_bp

    register_settings_cli(app)
    for blueprint in (health_bp, auth_bp, dashboard_bp, research_bp, editor_bp, settings_bp):
        app.register_blueprint(blueprint)


if __name__ == "__main__":
    create_app().run(debug=False)
