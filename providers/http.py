"""Small, safe HTTP helper for provider calls.

* Hard timeouts (connect + read) on every request.
* Redirects are never followed: some APIs authenticate with custom headers that a redirect
  could forward to another host.
* The response body is size-capped.
* Every network failure becomes a :class:`ProviderError`, so nothing raw escapes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from providers.base import ErrorKind, ProviderError

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_RETRY_AFTER_S = 7 * 24 * 3600


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]  # lower-cased names
    body: Any  # parsed JSON when possible, else the text
    is_json: bool


def post_json(
    provider: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    timeout: float,
    model: str | None = None,
) -> HttpResult:
    try:
        with requests.post(
            url,
            headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
            timeout=(min(10.0, timeout), timeout),
            allow_redirects=False,
            stream=True,
        ) as response:
            raw = response.raw.read(MAX_BODY_BYTES + 1, decode_content=True)
            status = response.status_code
            response_headers = {k.lower(): v for k, v in response.headers.items()}
    except requests.Timeout as exc:
        raise ProviderError(
            ErrorKind.TRANSIENT, f"request timed out after {timeout:.0f}s", provider=provider, model=model
        ) from exc
    except requests.RequestException as exc:
        raise ProviderError(
            ErrorKind.TRANSIENT, f"network error ({type(exc).__name__})", provider=provider, model=model
        ) from exc

    if len(raw) > MAX_BODY_BYTES:
        raise ProviderError(
            ErrorKind.MALFORMED_RESPONSE,
            "response body too large",
            provider=provider,
            status=status,
            model=model,
        )
    text = raw.decode("utf-8", errors="replace")
    try:
        return HttpResult(status, response_headers, json.loads(text), True)
    except ValueError:
        return HttpResult(status, response_headers, text[:500], False)


# ------------------------------------------------------------------- parsing helpers
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)")


def parse_duration(text: str) -> float | None:
    """Parse '34s', '1m2.5s', '2m59.56s', '7.66s', '120ms', '1h2m' into seconds."""
    parts = _DURATION_PART.findall(text.replace(" ", ""))
    if not parts:
        return None
    factors = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    total = sum(float(number) * factors[unit] for number, unit in parts)
    return min(total, MAX_RETRY_AFTER_S)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header: delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        pass
    try:
        from datetime import datetime

        delta = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        return min(max(delta, 0.0), MAX_RETRY_AFTER_S)
    except (TypeError, ValueError):
        return None


def short(text: object, limit: int = 300) -> str:
    return " ".join(str(text).split())[:limit]
