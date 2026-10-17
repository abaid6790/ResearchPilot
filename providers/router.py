"""LLM router: key rotation inside a provider, fallback across providers, bounded retries.

Decision table (what happens on each failure kind) is documented in docs/API_KEY_ROTATION.md:

==================  =======================================================================
Failure kind        Action
==================  =======================================================================
RATE_LIMIT          cool this key down (Retry-After if given), try the provider's next key
QUOTA_EXHAUSTED     cool the key (and its quota group) until the quota resets, next key
AUTH_INVALID        disable the key for a long time, next key
TRANSIENT           retry the same key with backoff (bounded); then next *provider*
MALFORMED_RESPONSE  retry the same key (bounded); then next provider; never rotates keys
CONTEXT_TOO_LONG    next provider (other keys of the same provider have the same limit)
CONTENT_BLOCKED     next provider
UNSUPPORTED         next provider
BAD_REQUEST         raise :class:`LLMRequestError` immediately; no key or provider will fix it
==================  =======================================================================

Everything is bounded: total attempts, retries per key, passes over the providers, inline
waiting time and an overall deadline, so a request can never loop forever.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from providers.base import (
    KEY_SCOPED_KINDS,
    AttemptFailure,
    ErrorKind,
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMUnavailableError,
    Message,
    NoProviderConfiguredError,
    ProviderAdapter,
    ProviderError,
    RawResult,
    StructuredOutputError,
    UsageRecord,
)
from providers.key_manager import KeyInfo, KeyManager
from providers.models import ModelCatalog, ModelSpec
from providers.structured import parse_structured

logger = logging.getLogger(__name__)


@dataclass
class RouterConfig:
    provider_order: list[str] = field(default_factory=lambda: ["gemini", "groq", "openrouter"])
    max_total_attempts: int = 8  # provider calls per request, across everything
    max_retries_per_key: int = 2  # extra tries on the same key for transient/malformed errors
    max_validation_retries: int = 1  # "repair" tries when JSON does not match the schema
    max_passes: int = 2  # passes over the provider list (a 2nd one only after a short wait)
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    max_inline_wait_s: float = 5.0  # longest we sleep inside a request waiting for a cooldown
    request_timeout_s: float = 60.0
    overall_deadline_s: float = 180.0
    max_concurrency: int = 8  # simultaneous in-flight calls per provider in this process


class _BudgetExhausted(Exception):
    """Internal: attempts or time ran out for this request."""


class _State:
    """Mutable bookkeeping for one ``generate`` call."""

    def __init__(self, request: LLMRequest, deadline: float):
        self.request = request
        self.deadline = deadline
        self.attempts = 0
        self.failures: list[AttemptFailure] = []


class LLMRouter:
    def __init__(
        self,
        adapters: dict[str, ProviderAdapter],
        keys: KeyManager,
        catalog: ModelCatalog,
        config: RouterConfig | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        usage_recorder: Callable[[UsageRecord], None] | None = None,
    ) -> None:
        self.adapters = adapters
        self.keys = keys
        self.catalog = catalog
        self.config = config or RouterConfig()
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()  # noqa: S311 - backoff jitter, not security
        self.usage_recorder = usage_recorder
        self._slots = {p: threading.BoundedSemaphore(self.config.max_concurrency) for p in adapters}
        self._lock = threading.Lock()
        self._metrics = {"requests": 0, "succeeded": 0, "failed": 0, "attempts": 0, "fallbacks": 0}

    # --------------------------------------------------------------- public API
    def active_providers(self) -> list[str]:
        """Configured providers (in fallback order) that have an adapter and at least one key."""
        return [p for p in self.config.provider_order if p in self.adapters and self.keys.has_keys(p)]

    def metrics(self) -> dict:
        with self._lock:
            return dict(self._metrics)

    def generate(self, request: LLMRequest) -> LLMResponse:
        order = self.active_providers()
        if not order:
            raise NoProviderConfiguredError(
                "No LLM API keys are configured. Set GEMINI_API_KEY_1, GROQ_API_KEY_1 "
                "or OPENROUTER_API_KEY_1.",
                [],
            )
        self._count("requests")
        state = _State(request, self.clock() + self.config.overall_deadline_s)
        settled: set[str] = set()  # providers that failed for reasons a wait will not fix
        try:
            for pass_no in range(max(1, self.config.max_passes)):
                for index, provider in enumerate(order):
                    if provider in settled:
                        continue
                    response = self._try_provider(provider, state, settled)
                    if response is not None:
                        if index > 0 or pass_no > 0:
                            self._count("fallbacks")
                        self._count("succeeded")
                        return response
                if pass_no + 1 >= self.config.max_passes or not self._wait_for_cooldown(
                    order, settled, state
                ):
                    break
        except _BudgetExhausted:
            pass
        except LLMRequestError:
            self._count("failed")
            raise
        self._count("failed")
        raise self._unavailable(order, state)

    def probe(self, provider: str, key_label: str | None = None, tier: str = "fast") -> list[dict]:
        """One real call per key (cooldowns ignored) to check that keys and models work.

        Results are reported to the key manager like any other call. Used by ``flask llm-ping``.
        """
        adapter = self.adapters.get(provider)
        if adapter is None:
            raise ValueError(f"unknown provider {provider!r}")
        request = LLMRequest.simple(
            "Reply with the single word: pong", tier=tier, task="ping", max_output_tokens=64
        )
        spec = self.catalog.select(provider, request)
        results = []
        for key in self.keys.all_keys(provider):
            if key_label and key.label != key_label:
                continue
            started = self.clock()
            row = {"provider": provider, "label": key.label, "model": spec.id if spec else None}
            if spec is None:
                results.append(
                    {**row, "ok": False, "kind": "unsupported", "status": None, "message": "no model"}
                )
                continue
            try:
                with self._slots[provider]:
                    adapter.generate(
                        request,
                        api_key=key.secret,
                        model=spec,
                        timeout=min(30.0, self.config.request_timeout_s),
                    )
                self.keys.report_success(key)
                row.update(ok=True, kind=None, status=200, message="ok")
            except ProviderError as exc:
                if exc.kind in KEY_SCOPED_KINDS:
                    self.keys.report_failure(
                        key, exc.kind, retry_after=exc.retry_after, scope=exc.quota_scope
                    )
                row.update(ok=False, kind=exc.kind.value, status=exc.status, message=str(exc))
            row["latency_s"] = round(self.clock() - started, 3)
            results.append(row)
        return results

    # ------------------------------------------------------------------ internals
    def _count(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._metrics[name] += amount

    def _record(
        self,
        state: _State,
        provider: str,
        key: KeyInfo | None,
        spec: ModelSpec | None,
        outcome: str,
        started: float,
        raw: RawResult | None = None,
        status: int | None = None,
    ) -> None:
        if self.usage_recorder is None:
            return
        try:
            self.usage_recorder(
                UsageRecord(
                    request_id=state.request.request_id,
                    task=state.request.task,
                    provider=provider,
                    model=spec.id if spec else None,
                    key_label=key.label if key else None,
                    outcome=outcome,
                    latency_s=round(self.clock() - started, 3),
                    prompt_tokens=raw.usage.prompt_tokens if raw else None,
                    completion_tokens=raw.usage.completion_tokens if raw else None,
                    http_status=status,
                    user_id=state.request.user_id,
                    job_id=state.request.job_id,
                )
            )
        except Exception:  # noqa: BLE001 - recording must never break a request
            logger.exception("usage recorder failed")

    def _fail(
        self,
        state: _State,
        provider: str,
        key: KeyInfo | None,
        spec: ModelSpec | None,
        kind: ErrorKind,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        state.failures.append(
            AttemptFailure(
                provider,
                key.label if key else None,
                spec.id if spec else None,
                kind,
                status,
                message,
                retry_after,
            )
        )

    def _backoff(self, retry_index: int) -> float:
        ceiling = min(self.config.backoff_cap_s, self.config.backoff_base_s * (2**retry_index))
        return ceiling / 2 + self.rng.uniform(0, ceiling / 2)  # "equal jitter"

    def _sleep_within_budget(self, seconds: float, state: _State) -> None:
        remaining = state.deadline - self.clock()
        if seconds >= remaining:
            raise _BudgetExhausted
        if seconds > 0:
            self.sleep(seconds)

    def _wait_for_cooldown(self, order: list[str], settled: set[str], state: _State) -> bool:
        """Sleep until the soonest cooling key returns, if that is short enough. True if waited."""
        now = self.clock()
        times = [t for p in order if p not in settled and (t := self.keys.next_available_at(p)) is not None]
        if not times:
            return False
        wait = min(times) - now
        if wait > self.config.max_inline_wait_s or now + wait + 0.05 >= state.deadline:
            return False
        if wait > 0:
            self.sleep(wait + 0.05)
        return True

    def _unavailable(self, order: list[str], state: _State) -> LLMUnavailableError:
        retry_times = [t for p in order if (t := self.keys.next_available_at(p)) is not None]
        retry_at = min(retry_times) if retry_times else None
        if state.failures:
            parts = []
            for f in state.failures[-6:]:
                where = f.key_label or f.provider
                parts.append(f"{where}: {f.kind.value}" + (f" (HTTP {f.status})" if f.status else ""))
            detail = "; ".join(parts)
        else:
            detail = "all keys are cooling down or disabled"
        return LLMUnavailableError(
            f"No LLM provider could serve the request ({detail}).", state.failures, retry_at
        )

    def _try_provider(self, provider: str, state: _State, settled: set[str]) -> LLMResponse | None:
        if self.keys.breaker_open(provider):
            self._fail(
                state, provider, None, None, ErrorKind.TRANSIENT, "provider paused after repeated failures"
            )
            return None
        spec = self.catalog.select(provider, state.request)
        if spec is None:
            self._fail(
                state,
                provider,
                None,
                None,
                ErrorKind.CONTEXT_TOO_LONG,
                "input too large for this provider's models",
            )
            settled.add(provider)
            return None
        candidates = self.keys.candidates(provider)
        if not candidates:
            self._fail(state, provider, None, spec, ErrorKind.RATE_LIMIT, "all keys cooling down or disabled")
            return None
        for key in candidates:
            if not self.keys.is_available(key):  # cooled meanwhile (e.g. a shared quota group)
                continue
            response, action = self._try_key(provider, key, spec, state)
            if response is not None:
                return response
            if action == "next_provider":
                settled.add(provider)
                return None
        return None  # every key was rotated away from; cooldowns decide whether a later pass helps

    def _try_key(
        self, provider: str, key: KeyInfo, spec: ModelSpec, state: _State
    ) -> tuple[LLMResponse | None, str]:
        """Returns ``(response, "")`` on success, else ``(None, "next_key" | "next_provider")``."""
        cfg = self.config
        adapter = self.adapters[provider]
        request = state.request
        retries = 0
        validation_retries = 0
        while True:
            if state.attempts >= cfg.max_total_attempts:
                raise _BudgetExhausted
            timeout = min(request.timeout_s or cfg.request_timeout_s, state.deadline - self.clock())
            if timeout <= 0:
                raise _BudgetExhausted
            state.attempts += 1
            self._count("attempts")
            started = self.clock()
            try:
                with self._slots[provider]:
                    raw = adapter.generate(request, api_key=key.secret, model=spec, timeout=timeout)
            except ProviderError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - an adapter bug must not take the app down
                logger.exception("unexpected error in %s adapter", provider)
                error = ProviderError(
                    ErrorKind.MALFORMED_RESPONSE,
                    f"unexpected adapter error ({type(exc).__name__})",
                    provider=provider,
                    model=spec.id,
                )
            else:
                self.keys.report_success(key)  # HTTP-level success: the key works
                parsed = None
                if request.response_model is not None:
                    try:
                        parsed = parse_structured(raw.text, request.response_model)
                    except StructuredOutputError as exc:
                        self._record(state, provider, key, spec, "invalid_output", started, raw, 200)
                        self.keys.note_failure_metric(key, ErrorKind.MALFORMED_RESPONSE)
                        self._fail(state, provider, key, spec, ErrorKind.MALFORMED_RESPONSE, str(exc), 200)
                        if validation_retries < cfg.max_validation_retries:
                            validation_retries += 1
                            request = self._repair_request(state.request, raw.text, str(exc))
                            continue
                        return None, "next_provider"
                self._record(state, provider, key, spec, "ok", started, raw, 200)
                return (
                    LLMResponse(
                        text=raw.text,
                        provider=provider,
                        model=spec.id,
                        key_label=key.label,
                        usage=raw.usage,
                        latency_s=round(self.clock() - started, 3),
                        attempts=state.attempts,
                        request_id=state.request.request_id,
                        finish_reason=raw.finish_reason,
                        parsed=parsed,
                    ),
                    "",
                )

            # ---- failure handling
            self._record(state, provider, key, spec, error.kind.value, started, None, error.status)
            self._fail(state, provider, key, spec, error.kind, str(error), error.status, error.retry_after)
            kind = error.kind

            if kind is ErrorKind.BAD_REQUEST:
                raise LLMRequestError(error)
            if kind in KEY_SCOPED_KINDS:
                self.keys.report_failure(key, kind, retry_after=error.retry_after, scope=error.quota_scope)
                return None, "next_key"
            if kind in (ErrorKind.TRANSIENT, ErrorKind.MALFORMED_RESPONSE):
                hint = error.retry_after
                if retries < cfg.max_retries_per_key and not (hint and hint > cfg.max_inline_wait_s):
                    delay = self._backoff(retries)
                    if hint:
                        delay = max(delay, hint)
                    retries += 1
                    self._sleep_within_budget(delay, state)
                    continue
                self.keys.note_failure_metric(key, kind)
                if kind is ErrorKind.TRANSIENT:
                    self.keys.report_transient(provider)
                return None, "next_provider"
            # CONTEXT_TOO_LONG, CONTENT_BLOCKED, UNSUPPORTED: another key will not change the outcome
            self.keys.note_failure_metric(key, kind)
            return None, "next_provider"

    @staticmethod
    def _repair_request(original: LLMRequest, bad_reply: str, problem: str) -> LLMRequest:
        """Ask the model to fix its own invalid JSON (a single, bounded retry)."""
        clipped = bad_reply if len(bad_reply) <= 4000 else bad_reply[:4000] + " ..."
        messages = [
            *original.messages,
            Message("assistant", clipped),
            Message(
                "user", f"That reply was not valid. {problem}\nReply again with only the corrected JSON."
            ),
        ]
        return replace(original, messages=messages)
