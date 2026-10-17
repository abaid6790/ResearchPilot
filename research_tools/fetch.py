"""Safe page fetching.

Every request goes through a guarded session (``net.make_guarded_session``): addresses are
validated and pinned at connect time, redirects are followed manually and each hop is re-checked,
the body is size-capped *after* decompression, and the whole fetch has a time budget.

Limitations (see docs/SECURITY.md): robots.txt is not consulted; the fetcher identifies itself
honestly as ResearchPilotBot; JavaScript is not executed, so pages that render client-side
return little text.
"""

from __future__ import annotations

import time
import zlib
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests
import urllib3.exceptions

from research_tools import net
from research_tools.net import UnsafeURLError

USER_AGENT = "ResearchPilotBot/1.0 (+research assistant; honest bot)"
ALLOWED_CONTENT_TYPES = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
    "application/pdf": "pdf",
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass
class FetchSettings:
    max_bytes: int = 5 * 1024 * 1024
    connect_timeout_s: float = 8.0
    read_timeout_s: float = 15.0
    total_timeout_s: float = 30.0
    max_redirects: int = 5

    @classmethod
    def from_env(cls, environ) -> FetchSettings:
        def num(name, default, lo, hi):
            try:
                return min(hi, max(lo, float((environ.get(name) or "").strip() or default)))
            except ValueError:
                return default

        return cls(
            max_bytes=int(num("FETCH_MAX_BYTES", 5 * 1024 * 1024, 100_000, 25 * 1024 * 1024)),
            total_timeout_s=num("FETCH_TIMEOUT_S", 30.0, 5, 120),
            max_redirects=int(num("FETCH_MAX_REDIRECTS", 5, 0, 10)),
        )


@dataclass
class FetchResult:
    ok: bool
    url: str
    final_url: str | None = None
    status: int | None = None
    kind: str | None = None  # "html" | "text" | "pdf"
    content_type: str | None = None
    body: bytes = b""
    encoding: str | None = None
    redirected: bool = False
    error: str | None = None  # short, user-safe reason
    reason: str | None = None  # machine reason: unsafe, timeout, too_large, bad_type, http_error, network
    redirects: list[str] = field(default_factory=list)


class SafeFetcher:
    def __init__(
        self, settings: FetchSettings | None = None, *, session: requests.Session | None = None, **net_kwargs
    ):
        self.settings = settings or FetchSettings()
        self._ports = net_kwargs.get("ports", net.ALLOWED_PORTS)
        self._session = session or net.make_guarded_session(**net_kwargs)

    # ------------------------------------------------------------------ public API
    def fetch(self, url: str) -> FetchResult:
        s = self.settings
        deadline = time.monotonic() + s.total_timeout_s
        current = url
        chain: list[str] = []
        try:
            for _hop in range(s.max_redirects + 1):
                net.check_url(current, ports=self._ports)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return FetchResult(False, url, error="took too long", reason="timeout")
                response = self._session.get(
                    current,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.9,*/*;q=0.1",
                        "Accept-Encoding": "gzip, deflate",
                    },
                    timeout=(min(s.connect_timeout_s, remaining), min(s.read_timeout_s, remaining)),
                    allow_redirects=False,
                    stream=True,
                )
                try:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            return FetchResult(
                                False,
                                url,
                                status=response.status_code,
                                error="redirect without target",
                                reason="http_error",
                            )
                        current = urljoin(current, location)
                        chain.append(current)
                        continue
                    return self._read(url, current, response, deadline, chain)
                finally:
                    response.close()
            return FetchResult(False, url, error="too many redirects", reason="redirects", redirects=chain)
        except UnsafeURLError as exc:
            return FetchResult(False, url, error=f"blocked: {exc}", reason="unsafe", redirects=chain)
        except requests.exceptions.ConnectionError as exc:
            # NewConnectionError raised by the guard is wrapped by urllib3/requests
            if "blocked:" in str(exc):
                return FetchResult(
                    False,
                    url,
                    error="blocked: host resolves to a non-public address",
                    reason="unsafe",
                    redirects=chain,
                )
            return FetchResult(False, url, error="could not connect", reason="network", redirects=chain)
        except requests.Timeout:
            return FetchResult(False, url, error="timed out", reason="timeout", redirects=chain)
        except requests.RequestException as exc:
            return FetchResult(
                False, url, error=f"network error ({type(exc).__name__})", reason="network", redirects=chain
            )

    def probe(self, url: str) -> tuple[str, int | None]:
        """Link status for a URL without keeping the body: (link_status, http_status)."""
        result = self.fetch(url)
        if result.reason == "unsafe":
            return "unsafe", None
        if result.status is not None and result.status < 400 and result.error is None or result.ok:
            return ("redirected" if result.redirected else "reachable"), result.status
        if result.reason in ("bad_type", "too_large") and result.status and result.status < 400:
            return ("redirected" if result.redirected else "reachable"), result.status
        return "unreachable", result.status

    # ----------------------------------------------------------------- internals
    def _read(self, original: str, current: str, response, deadline: float, chain: list[str]) -> FetchResult:
        status = response.status_code
        redirected = bool(chain)
        base = {
            "url": original,
            "final_url": current,
            "status": status,
            "redirected": redirected,
            "redirects": chain,
        }
        if status in (401, 402, 403) or status == 451:
            return FetchResult(False, error=f"access refused (HTTP {status})", reason="http_error", **base)
        if status >= 400:
            return FetchResult(False, error=f"HTTP {status}", reason="http_error", **base)
        ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        kind = ALLOWED_CONTENT_TYPES.get(ctype)
        if kind is None:
            return FetchResult(
                False, content_type=ctype or None, error="unsupported content type", reason="bad_type", **base
            )
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > self.settings.max_bytes:
            return FetchResult(
                False, content_type=ctype, error="page is too large", reason="too_large", **base
            )
        encoding = (response.headers.get("Content-Encoding") or "").lower().strip()
        try:
            body = self._read_body(response, encoding, deadline)
        except _TooLarge:
            return FetchResult(
                False, content_type=ctype, error="page is too large", reason="too_large", **base
            )
        except _TooSlow:
            return FetchResult(False, content_type=ctype, error="took too long", reason="timeout", **base)
        except (zlib.error, requests.exceptions.ContentDecodingError, urllib3.exceptions.HTTPError):
            return FetchResult(
                False, content_type=ctype, error="could not decode the response", reason="network", **base
            )
        charset = response.encoding if ctype != "application/pdf" else None
        declared_charset = None
        if "charset=" in (response.headers.get("Content-Type") or "").lower():
            declared_charset = charset
        return FetchResult(True, kind=kind, content_type=ctype, body=body, encoding=declared_charset, **base)

    def _read_body(self, response, encoding: str, deadline: float) -> bytes:
        """Read at most ``max_bytes`` of *decoded* data, so compressed bombs are cut off."""
        limit = self.settings.max_bytes
        chunks: list[bytes] = []
        total = 0
        for chunk in response.raw.stream(16384, decode_content=True):
            total += len(chunk)
            if total > limit:
                raise _TooLarge
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise _TooSlow
        return b"".join(chunks)


class _TooLarge(Exception):
    pass


class _TooSlow(Exception):
    pass
