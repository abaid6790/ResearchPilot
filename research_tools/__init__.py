"""Research tools: web search (Tavily), safe page fetching, document reading, source preparation.

Everything fetched or uploaded is *untrusted*. See ``research_tools/untrusted.py`` and
docs/SECURITY.md for how it is handled.
"""

from __future__ import annotations

import os
import threading

_lock = threading.Lock()


def get_search_client(app=None):
    """Process-wide Tavily client for the current app, or ``None`` when no key is configured."""
    from flask import current_app

    from research_tools.search import TavilyClient

    app = app or current_app._get_current_object()  # noqa: SLF001
    with _lock:
        if "search_client" not in app.extensions:
            environ = app.config.get("TOOLS_ENVIRON")
            if environ is None:
                environ = {} if app.config.get("TESTING") else os.environ
            recorder = None
            if app.config.get("LLM_RECORD_USAGE", not app.config.get("TESTING")):
                from providers.usage import make_db_recorder

                recorder = make_db_recorder(app)
            app.extensions["search_client"] = TavilyClient.from_env(environ, usage_recorder=recorder)
        return app.extensions["search_client"]


def init_app(app) -> None:
    from research_tools.cli import register_cli

    register_cli(app)
    from agents.cli import register_cli as register_agent_cli

    register_agent_cli(app)
