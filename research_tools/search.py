"""Tavily web search client (direct HTTP, bounded retries, no secrets in logs).

API reference: https://docs.tavily.com (POST /search, ``Authorization: Bearer``). Tavily's
``published_date`` is its own estimate and is kept only when it parses as a real past date;
it is never invented. Results are *candidates*: finding a URL does not verify any claim.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from providers.base import ErrorKind, ProviderError, UsageRecord
from providers.http import parse_retry_after, post_json, short
from research_tools.extract import parse_date
from research_tools.urls import canonicalize, domain_matches, domain_of

logger = logging.getLogger(__name__)
PLACEHOLDERS = {"", "changeme", "your-key-here", "your_key_here", "xxx"}
DEFAULT_BASE_URL = "https://api.tavily.com"


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    score: float | None
    published_at: datetime | None = None
    raw_content: str | None = None


@dataclass
class SearchResponse:
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    request_id: str | None = None
    latency_s: float = 0.0


class SearchUnavailableError(Exception):
    """Search could not be completed; ``kind`` says why (an ErrorKind value) and ``retry_after``
    is in seconds when the provider said so."""

    def __init__(self, message: str, kind: ErrorKind, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def classify(status: int, body, headers: dict[str, str]) -> ProviderError:
    detail = ""
    if isinstance(body, dict):
        d = body.get("detail")
        detail = d.get("error") if isinstance(d, dict) else (d if isinstance(d, str) else "")
        detail = detail or str(body.get("error") or "")
    elif isinstance(body, str):
        detail = body
    msg = f"Tavily HTTP {status}: {short(detail, 200)}" if detail else f"Tavily HTTP {status}"
    retry_after = parse_retry_after(headers.get("retry-after"))
    if status == 401 or status == 403:
        kind = ErrorKind.AUTH_INVALID
    elif status == 429:
        kind = ErrorKind.RATE_LIMIT
    elif status in (432, 433):
        kind = ErrorKind.QUOTA_EXHAUSTED
    elif status in (400, 422):
        kind = ErrorKind.BAD_REQUEST
    elif status >= 500 or status in (408, 409):
        kind = ErrorKind.TRANSIENT
    else:
        kind = ErrorKind.BAD_REQUEST
    return ProviderError(kind, msg, provider="tavily", status=status, retry_after=retry_after)


class TavilyClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        usage_recorder: Callable[[UsageRecord], None] | None = None,
        max_inline_wait_s: float = 10.0,
    ):
        key = (api_key or "").strip()
        self._key = None if key.lower() in PLACEHOLDERS else key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._sleep = sleep
        self._record = usage_recorder
        self.max_inline_wait_s = max_inline_wait_s

    def __repr__(self) -> str:  # never show the key
        return f"TavilyClient(configured={self.configured})"

    @classmethod
    def from_env(cls, environ: Mapping[str, str], **kwargs) -> TavilyClient:
        def num(name, default):
            try:
                return float((environ.get(name) or "").strip() or default)
            except ValueError:
                return default

        return cls(
            environ.get("TAVILY_API_KEY"),
            base_url=(environ.get("TAVILY_BASE_URL") or "").strip() or DEFAULT_BASE_URL,
            timeout_s=num("TAVILY_TIMEOUT_S", 30.0),
            **kwargs,
        )

    @property
    def configured(self) -> bool:
        return self._key is not None

    def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        depth: str = "basic",
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        time_range: str | None = None,
        user_id: int | None = None,
        job_id: int | None = None,
    ) -> SearchResponse:
        if not self._key:
            raise SearchUnavailableError(
                "Web search is not configured (TAVILY_API_KEY is missing).", ErrorKind.AUTH_INVALID
            )
        query = " ".join(query.split())[:400]
        if not query:
            raise SearchUnavailableError("Empty search query.", ErrorKind.BAD_REQUEST)
        payload: dict = {
            "query": query,
            "search_depth": depth if depth in ("basic", "advanced") else "basic",
            "max_results": max(1, min(int(max_results), 20)),
            "include_answer": False,
            "include_raw_content": False,
        }
        if include_domains:
            payload["include_domains"] = include_domains[:300]
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains[:150]
        if time_range in ("day", "week", "month", "year"):
            payload["time_range"] = time_range
        headers = {"Authorization": f"Bearer {self._key}"}
        url = f"{self.base_url}/search"

        last: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                result = post_json(
                    "tavily",
                    url,
                    headers=headers,
                    payload=payload,
                    timeout=self.timeout_s,
                    model=payload["search_depth"],
                )
            except ProviderError as exc:
                self._usage(exc.kind.value, started, None, payload, user_id, job_id)
                last = exc
            else:
                if (
                    result.status == 200
                    and result.is_json
                    and isinstance(result.body, dict)
                    and isinstance(result.body.get("results"), list)
                ):
                    self._usage("ok", started, 200, payload, user_id, job_id)
                    return self._parse(query, result.body, time.monotonic() - started, exclude_domains or [])
                if result.status == 200:
                    last = ProviderError(
                        ErrorKind.MALFORMED_RESPONSE,
                        "Tavily returned an unusable response",
                        provider="tavily",
                        status=200,
                    )
                else:
                    last = classify(result.status, result.body, result.headers)
                self._usage(last.kind.value, started, result.status, payload, user_id, job_id)

            if last.kind not in (ErrorKind.TRANSIENT, ErrorKind.MALFORMED_RESPONSE, ErrorKind.RATE_LIMIT):
                break
            if attempt >= self.max_retries:
                break
            wait = (
                last.retry_after
                if last.retry_after is not None
                else min(8.0, 0.5 * 2**attempt) * (0.5 + random.random() / 2)  # noqa: S311
            )
            if wait > self.max_inline_wait_s:
                break  # never sleep for long inside a request; report when to retry instead
            self._sleep(wait)

        assert last is not None
        raise SearchUnavailableError(str(last).replace(self._key, "[redacted]"), last.kind, last.retry_after)

    # ------------------------------------------------------------------ internals
    def _usage(self, outcome, started, status, payload, user_id, job_id) -> None:
        if self._record is None:
            return
        try:
            self._record(
                UsageRecord(
                    request_id=f"s{int(started * 1000) % 10**9:09d}{random.randrange(10**5):05d}",  # noqa: S311
                    task="search", provider="tavily", model=payload["search_depth"], key_label="tavily#1",
                    outcome=outcome, latency_s=time.monotonic() - started, http_status=status,
                    user_id=user_id, job_id=job_id,
                )
            )  # fmt: skip
        except Exception:  # noqa: BLE001
            logger.warning("Could not record search usage")

    @staticmethod
    def _parse(query: str, body: dict, latency: float, exclude: list[str]) -> SearchResponse:
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for item in body["results"]:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str):
                continue
            try:
                canonical = canonicalize(url)
            except ValueError:
                continue
            if canonical in seen or (exclude and domain_matches(domain_of(canonical), exclude)):
                continue
            seen.add(canonical)
            score = item.get("score")
            hits.append(
                SearchHit(
                    title=" ".join(str(item.get("title") or "").split())[:500] or domain_of(canonical),
                    url=url[:2048],
                    snippet=str(item.get("content") or "")[:4000],
                    score=float(score) if isinstance(score, (int, float)) and 0 <= score <= 1 else None,
                    published_at=parse_date(item.get("published_date"))
                    if isinstance(item.get("published_date"), str)
                    else None,
                )
            )
        rid = body.get("request_id")
        return SearchResponse(query, hits, rid if isinstance(rid, str) else None, latency)
