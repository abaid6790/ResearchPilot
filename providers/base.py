"""Shared types for the LLM provider layer: requests, responses, errors, adapter interface."""

from __future__ import annotations

import enum
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.logging_utils import redact

if TYPE_CHECKING:
    from pydantic import BaseModel

    from providers.models import ModelSpec


# --------------------------------------------------------------------------- errors
class ErrorKind(enum.StrEnum):
    """What went wrong, in terms of what the router should do about it."""

    RATE_LIMIT = "rate_limit"  # temporary: per-minute requests/tokens, concurrency
    QUOTA_EXHAUSTED = "quota_exhausted"  # daily quota or credits used up
    AUTH_INVALID = "auth_invalid"  # key rejected / no permission
    TRANSIENT = "transient"  # timeout, network, 5xx, overloaded
    BAD_REQUEST = "bad_request"  # our request is wrong; no key or provider will fix it
    CONTEXT_TOO_LONG = "context_too_long"  # this model cannot take the input
    CONTENT_BLOCKED = "content_blocked"  # safety / policy refusal by this provider
    MALFORMED_RESPONSE = "malformed_response"  # unusable body from the provider
    UNSUPPORTED = "unsupported"  # model or feature not available on this account


#: Kinds where switching to another *key* of the same provider can help.
KEY_SCOPED_KINDS = frozenset({ErrorKind.RATE_LIMIT, ErrorKind.QUOTA_EXHAUSTED, ErrorKind.AUTH_INVALID})


class LLMError(Exception):
    """Base class for everything this package raises on purpose."""


class ProviderError(LLMError):
    """A single failed call to a provider. The message is redacted and never contains keys."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        provider: str,
        status: int | None = None,
        retry_after: float | None = None,
        quota_scope: str | None = None,  # "minute", "day", "credits" or None when unknown
        model: str | None = None,
    ):
        super().__init__(redact(message)[:400])
        self.kind = kind
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.quota_scope = quota_scope
        self.model = model


class LLMRequestError(LLMError):
    """The request itself was rejected (HTTP 400-class). Retrying elsewhere will not help."""

    def __init__(self, cause: ProviderError):
        super().__init__(f"{cause.provider} rejected the request: {cause}")
        self.cause = cause


class StructuredOutputError(LLMError):
    """The model replied, but not with JSON matching the requested schema."""


@dataclass
class AttemptFailure:
    provider: str
    key_label: str | None
    model: str | None
    kind: ErrorKind
    status: int | None
    message: str
    retry_after: float | None = None


class LLMUnavailableError(LLMError):
    """No configured provider could serve the request within the retry budget."""

    def __init__(self, message: str, failures: list[AttemptFailure], retry_at: float | None = None):
        super().__init__(message)
        self.failures = failures
        self.retry_at = retry_at  # epoch seconds when something may work again, if known

    @property
    def all_malformed(self) -> bool:
        return bool(self.failures) and all(f.kind is ErrorKind.MALFORMED_RESPONSE for f in self.failures)


class NoProviderConfiguredError(LLMUnavailableError):
    """No API keys are configured for any enabled provider."""


# ------------------------------------------------------------------- request/response
@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str


@dataclass
class LLMRequest:
    messages: list[Message]
    system: str | None = None
    task: str = "general"  # planner, researcher, analyst, writer, critic, validator, ...
    tier: str | None = None  # "quality" or "fast"; derived from ``task`` when omitted
    temperature: float | None = None  # None = provider default (recommended for Gemini 3.x)
    max_output_tokens: int | None = None
    response_model: type[BaseModel] | None = None
    timeout_s: float | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user_id: int | None = None  # for usage accounting only; never sent to a provider
    job_id: int | None = None

    @classmethod
    def simple(cls, prompt: str, *, system: str | None = None, **kwargs) -> LLMRequest:
        return cls(messages=[Message("user", prompt)], system=system, **kwargs)


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class RawResult:
    """What an adapter returns for one successful call."""

    text: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    key_label: str
    usage: Usage
    latency_s: float
    attempts: int
    request_id: str
    finish_reason: str | None = None
    parsed: BaseModel | None = None


@dataclass
class UsageRecord:
    """One provider call (successful or not), for later persistence. Never contains secrets."""

    request_id: str
    task: str
    provider: str
    model: str | None
    key_label: str | None
    outcome: str  # "ok" or an ErrorKind value
    latency_s: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    http_status: int | None = None
    user_id: int | None = None
    job_id: int | None = None


# ----------------------------------------------------------------------- adapter ABC
class ProviderAdapter(ABC):
    """Translates one :class:`LLMRequest` into a provider's HTTP API and back."""

    name: str

    @abstractmethod
    def generate(self, request: LLMRequest, *, api_key: str, model: ModelSpec, timeout: float) -> RawResult:
        """Make one call. Must raise :class:`ProviderError` (never a raw HTTP/requests error)."""
