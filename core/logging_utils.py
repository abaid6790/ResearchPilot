"""Logging setup with secret redaction.

Every log record passes through :class:`RedactingFilter`, which removes
well-known API key shapes, bearer tokens, passwords in query strings, the raw
token segment of verification / reset URLs, and the exact values of any
configured secret environment variables.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), REDACTED),  # Google API keys
    (re.compile(r"gsk_[0-9A-Za-z]{20,}"), REDACTED),  # Groq
    (re.compile(r"sk-or-[0-9A-Za-z_\-]{16,}"), REDACTED),  # OpenRouter
    (re.compile(r"tvly-[0-9A-Za-z_\-]{16,}"), REDACTED),  # Tavily
    (re.compile(r"fc-[0-9A-Za-z]{20,}"), REDACTED),  # Firecrawl
    (re.compile(r"(?i)(bearer\s+)[0-9A-Za-z._\-]{8,}"), r"\1" + REDACTED),
    (
        re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)[^\s&,;'\"]+"),
        r"\1\2" + REDACTED,
    ),
    # Raw one-time tokens embedded in URL paths
    (re.compile(r"(/auth/(?:verify|reset)/)[A-Za-z0-9_\-]{16,}"), r"\1" + REDACTED),
    # Credentials inside connection URLs: scheme://user:password@host
    (re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s]+(@)"), r"\1" + REDACTED + r"\2"),
]

SECRET_ENV_NAMES = (
    "SECRET_KEY",
    "MAIL_PASSWORD",
    *(f"{provider}_API_KEY_{n}" for provider in ("GEMINI", "GROQ", "OPENROUTER") for n in range(1, 9)),
    "TAVILY_API_KEY",
    "FIRECRAWL_API_KEY",
)


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _env_secret_values() -> list[str]:
    values = [os.getenv(name, "") for name in SECRET_ENV_NAMES]
    return [v for v in values if len(v) >= 8]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        secrets = _env_secret_values()
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the app
            message = str(record.msg)
        record.msg = redact(message, secrets)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, secrets)
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Install a redacting handler on the root logger (idempotent)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if any(isinstance(f, RedactingFilter) for f in handler.filters):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.handlers = [handler]
    root.setLevel(level)
