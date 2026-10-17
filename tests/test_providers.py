"""LLM provider layer: key rotation, fallback, errors, retries, structured output, concurrency.

The adapters talk over real HTTP to ``tests/fake_providers.py``; time is faked so cooldown
behaviour is deterministic. No real provider is ever contacted and no real key is used.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict

import pytest
from pydantic import BaseModel

from providers import build_router
from providers.base import (
    ErrorKind,
    LLMRequest,
    LLMRequestError,
    LLMUnavailableError,
    NoProviderConfiguredError,
)
from providers.key_manager import next_pacific_midnight
from tests.fake_providers import (
    FakeProviders,
    Reply,
    gemini_ambiguous_429,
    gemini_daily_429,
    gemini_error,
    gemini_invalid_key,
    gemini_minute_429,
    gemini_ok,
    openai_error,
    openai_ok,
)

G = [f"AIzaSyFakeGeminiKeyNumber{n}ForTestsOnly000" for n in range(1, 5)]
GROQ = "gsk_FakeGroqKeyForTestsOnly1234567890"
OR = "sk-or-v1-FakeOpenRouterKeyForTestsOnly12345"
ALL_KEYS = [*G, GROQ, OR]
T0 = 1_800_000_000.0  # fixed "now" for the fake clock


class FakeClock:
    def __init__(self) -> None:
        self.now = T0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def fake():
    server = FakeProviders().start()
    yield server
    server.stop()


@pytest.fixture()
def clock():
    return FakeClock()


def make(fake, clock, *, gemini=4, groq=True, openrouter=True, usage_recorder=None, **extra):
    env = fake.env(**extra)
    for index in range(gemini):
        env[f"GEMINI_API_KEY_{index + 1}"] = G[index]
    if groq:
        env["GROQ_API_KEY_1"] = GROQ
    if openrouter:
        env["OPENROUTER_API_KEY_1"] = OR
    return build_router(env, usage_recorder=usage_recorder, clock=clock, sleep=clock.sleep)


def ask(router, prompt="hello", **kwargs):
    return router.generate(LLMRequest.simple(prompt, **kwargs))


def state_of(router, label):
    row = next(r for r in router.keys.snapshot() if r["label"] == label)
    return row


class Plan(BaseModel):
    title: str
    steps: list[str]


VALID_PLAN = json.dumps({"title": "T", "steps": ["a", "b"]})


# ------------------------------------------------------------- happy path & rotation
def test_happy_path_uses_first_provider_first_key(fake, clock):
    router = make(fake, clock)
    response = ask(router)
    assert (response.provider, response.key_label, response.text) == ("gemini", "gemini#1", "ok:gemini")
    assert response.usage.total_tokens == 8 and response.attempts == 1
    assert len(fake.calls) == 1
    # The key travels in a header, never in the URL.
    assert G[0] not in fake.calls[0].path and fake.calls[0].headers["x-goog-api-key"] == G[0]


def test_gemini_key1_rate_limited_then_key2_succeeds(fake, clock):
    fake.queue("gemini", G[0], [gemini_minute_429(retry_delay="7s")])
    router = make(fake, clock)

    response = ask(router)

    assert (response.provider, response.key_label) == ("gemini", "gemini#2")
    assert len(fake.calls_to("gemini", G[0])) == 1 and len(fake.calls_to("gemini", G[1])) == 1
    row = state_of(router, "gemini#1")
    assert row["status"] == "cooling_down" and row["reason"] == "rate_limit"
    assert 7 <= row["available_in_s"] <= 9  # the provider's retryDelay (+ small jitter), not hours

    ask(router)  # sticky: stays on the key that worked, does not retry the cooling key
    assert len(fake.calls_to("gemini", G[0])) == 1 and len(fake.calls_to("gemini", G[1])) == 2

    clock.advance(10)
    assert state_of(router, "gemini#1")["status"] == "available"


def test_multiple_gemini_keys_exhausted_fall_back_to_groq(fake, clock):
    for key in G:
        fake.queue("gemini", key, [gemini_daily_429()])
    router = make(fake, clock)

    response = ask(router)

    assert (response.provider, response.key_label) == ("groq", "groq#1")
    assert [len(fake.calls_to("gemini", k)) for k in G] == [1, 1, 1, 1]
    expected = next_pacific_midnight(clock.now) - clock.now
    for label in ("gemini#1", "gemini#2", "gemini#3", "gemini#4"):
        row = state_of(router, label)
        assert row["reason"] == "quota_exhausted" and abs(row["available_in_s"] - expected) <= 2

    ask(router)  # daily-exhausted keys are skipped without any more calls to Gemini
    assert len(fake.calls_to("gemini")) == 4 and len(fake.calls_to("groq")) == 2


def test_keys_from_the_same_project_share_daily_quota(fake, clock):
    fake.queue("gemini", G[0], [gemini_daily_429()])
    router = make(fake, clock, GEMINI_API_KEY_1_PROJECT="proj-a", GEMINI_API_KEY_2_PROJECT="proj-a")

    response = ask(router)

    assert response.key_label == "gemini#3"  # key 2 shares key 1's project quota: skipped untried
    assert len(fake.calls_to("gemini", G[1])) == 0
    assert state_of(router, "gemini#2")["reason"] == "quota_exhausted"


def test_gemini_to_groq_fallback_on_transient_errors_does_not_rotate_keys(fake, clock):
    fake.always("gemini", gemini_error(503, "UNAVAILABLE", "The model is overloaded."))
    router = make(fake, clock)

    response = ask(router)

    assert response.provider == "groq"
    assert len(fake.calls_to("gemini", G[0])) == 3  # 1 try + max_retries_per_key (2)
    assert len(fake.calls_to("gemini", G[1])) == 0  # 5xx is not a key problem: no rotation
    assert len(clock.sleeps) == 2 and all(0 < s <= 8 for s in clock.sleeps)  # bounded backoff
    assert all(state_of(router, f"gemini#{i}")["status"] == "available" for i in range(1, 5))


def test_groq_to_openrouter_fallback(fake, clock):
    for key in G:
        fake.queue("gemini", key, [gemini_daily_429()])
    fake.queue(
        "groq", GROQ, [openai_error(429, "Rate limit reached for model (TPM)", headers={"retry-after": "30"})]
    )
    router = make(fake, clock)

    response = ask(router)

    assert (response.provider, response.key_label) == ("openrouter", "openrouter#1")
    row = state_of(router, "groq#1")
    assert row["reason"] == "rate_limit" and 30 <= row["available_in_s"] <= 34


def test_all_providers_unavailable(fake, clock):
    for key in G:
        fake.queue("gemini", key, [gemini_daily_429()])
    fake.queue("groq", GROQ, [openai_error(429, "Rate limit reached ... tokens per day (TPD)")])
    fake.queue("openrouter", OR, [openai_error(402, "Insufficient credits")])
    router = make(fake, clock)

    with pytest.raises(LLMUnavailableError) as caught:
        ask(router)

    error = caught.value
    assert {f.provider for f in error.failures} == {"gemini", "groq", "openrouter"}
    assert error.retry_at is not None and error.retry_at > clock.now
    assert not any(secret in str(error) for secret in ALL_KEYS)

    calls_before = len(fake.calls)
    with pytest.raises(LLMUnavailableError):
        ask(router)  # everything is cooling: fails fast, no network calls, no sleeping
    assert len(fake.calls) == calls_before and clock.sleeps == []


def test_invalid_credentials_disable_the_key_and_rotate(fake, clock):
    fake.queue("gemini", G[0], [gemini_invalid_key()])
    router = make(fake, clock)

    response = ask(router)

    assert response.key_label == "gemini#2"
    row = state_of(router, "gemini#1")
    assert row["status"] == "disabled" and row["reason"] == "auth_invalid"
    ask(router)
    assert len(fake.calls_to("gemini", G[0])) == 1  # not retried


def test_all_credentials_invalid_reports_unavailable_without_retry_time(fake, clock):
    fake.always("gemini", gemini_invalid_key())
    fake.always("groq", openai_error(401, "Invalid API Key"))
    fake.always("openrouter", openai_error(401, "No auth credentials found"))
    router = make(fake, clock)

    with pytest.raises(LLMUnavailableError) as caught:
        ask(router)

    assert all(f.kind is ErrorKind.AUTH_INVALID for f in caught.value.failures)
    assert caught.value.retry_at is None  # disabled keys do not come back by waiting


# ------------------------------------------------------------ timeouts and malformed
def test_transient_timeout_is_retried_on_the_same_key(fake, clock):
    fake.queue("gemini", G[0], [Reply(200, gemini_ok().body, delay=0.8)])
    router = make(fake, clock)

    response = ask(router, timeout_s=0.25)

    assert (response.provider, response.key_label, response.attempts) == ("gemini", "gemini#1", 2)
    assert len(fake.calls_to("gemini", G[0])) == 2


def test_repeated_timeouts_fall_back_to_next_provider(fake, clock):
    fake.queue("gemini", G[0], [Reply(200, gemini_ok().body, delay=0.6)] * 3)
    router = make(fake, clock)

    response = ask(router, timeout_s=0.2)

    assert response.provider == "groq"
    assert state_of(router, "gemini#1")["status"] == "available"  # a timeout is not the key's fault


@pytest.mark.parametrize(
    "bad",
    [
        Reply(200, {"candidates": []}),
        Reply(200, "this is not json at all"),
        Reply(200, {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}),
        Reply(200, ["unexpected", "list"]),
    ],
)
def test_malformed_responses_retry_then_fall_back(fake, clock, bad):
    fake.always("gemini", bad)
    router = make(fake, clock)

    response = ask(router)

    assert response.provider == "groq"
    assert len(fake.calls_to("gemini", G[0])) == 3 and len(fake.calls_to("gemini", G[1])) == 0


def test_all_providers_malformed_hits_the_attempt_cap(fake, clock):
    fake.always("gemini", Reply(200, {"candidates": []}))
    fake.always("groq", Reply(200, {"choices": []}))
    fake.always("openrouter", Reply(200, "garbage"))
    router = make(fake, clock)

    with pytest.raises(LLMUnavailableError) as caught:
        ask(router)

    assert caught.value.all_malformed
    assert len(fake.calls) == 8  # max_total_attempts: bounded even when everything misbehaves


def test_openrouter_error_inside_http_200_is_classified(fake, clock):
    fake.always("openrouter", Reply(200, {"error": {"code": 429, "message": "Provider returned error"}}))
    router = make(fake, clock, LLM_PROVIDER_ORDER="openrouter")

    with pytest.raises(LLMUnavailableError) as caught:
        ask(router)

    assert caught.value.failures[0].kind is ErrorKind.RATE_LIMIT
    assert state_of(router, "openrouter#1")["status"] == "cooling_down"


# ------------------------------------------------------- non-key errors do not rotate
def test_bad_request_is_raised_and_never_rotated_or_retried(fake, clock):
    fake.always("gemini", gemini_error(400, "INVALID_ARGUMENT", "Invalid value at 'contents'"))
    router = make(fake, clock)

    with pytest.raises(LLMRequestError):
        ask(router)

    assert len(fake.calls) == 1


def test_content_block_goes_to_next_provider_without_rotating_keys(fake, clock):
    fake.always("gemini", Reply(200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    router = make(fake, clock)

    response = ask(router)

    assert response.provider == "groq" and len(fake.calls_to("gemini")) == 1


def test_context_too_long_goes_to_next_provider_without_rotating_keys(fake, clock):
    fake.always(
        "gemini",
        gemini_error(400, "INVALID_ARGUMENT", "The input token count exceeds the maximum number of tokens"),
    )
    router = make(fake, clock)

    response = ask(router)

    assert response.provider == "groq" and len(fake.calls_to("gemini")) == 1


def test_oversized_input_skips_models_that_cannot_hold_it(fake, clock):
    router = make(fake, clock, LLM_PROVIDER_ORDER="groq,openrouter")
    huge = "x" * 600_000  # ~170k tokens: more than Groq's 131k window, fine for OpenRouter's 262k

    response = ask(router, huge)

    assert response.provider == "openrouter" and len(fake.calls_to("groq")) == 0


def test_no_keys_configured(fake, clock):
    router = make(fake, clock, gemini=0, groq=False, openrouter=False)
    with pytest.raises(NoProviderConfiguredError):
        ask(router)


def test_only_one_provider_configured_works(fake, clock):
    router = make(fake, clock, gemini=0, groq=False)
    assert ask(router).provider == "openrouter"


# ------------------------------------------------------ retry limits and cooldowns
def test_ambiguous_429_is_not_treated_as_daily_quota(fake, clock):
    fake.queue("gemini", G[0], [gemini_ambiguous_429()])
    router = make(fake, clock)

    ask(router)

    row = state_of(router, "gemini#1")
    assert row["reason"] == "rate_limit" and row["available_in_s"] <= 40  # ~30s, not until midnight


def test_repeated_429s_escalate_the_cooldown(fake, clock):
    router = make(fake, clock, gemini=1)
    waits = []
    for _ in range(3):
        fake.queue("gemini", G[0], [gemini_ambiguous_429()])
        ask(router)  # falls back to groq
        waits.append(state_of(router, "gemini#1")["available_in_s"])
        clock.advance(waits[-1] + 1)
    assert waits[0] < waits[1] < waits[2]
    assert 55 <= waits[1] <= 70 and 115 <= waits[2] <= 135


def test_success_resets_the_escalation(fake, clock):
    router = make(fake, clock, gemini=1)
    fake.queue("gemini", G[0], [gemini_ambiguous_429()])
    ask(router)
    clock.advance(60)
    assert ask(router).provider == "gemini"  # recovered, streak cleared
    fake.queue("gemini", G[0], [gemini_ambiguous_429()])
    ask(router)
    assert state_of(router, "gemini#1")["available_in_s"] <= 40


def test_retry_after_header_is_honoured(fake, clock):
    fake.queue(
        "gemini",
        G[0],
        [
            Reply(
                429,
                {"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED"}},
                {"retry-after": "120"},
            )
        ],
    )
    router = make(fake, clock)
    ask(router)
    assert 120 <= state_of(router, "gemini#1")["available_in_s"] <= 135


def test_short_cooldown_is_waited_out_once_then_retried(fake, clock):
    fake.queue("gemini", G[0], [gemini_minute_429(retry_delay="2s")])
    router = make(fake, clock, gemini=1, LLM_PROVIDER_ORDER="gemini")

    response = ask(router)

    assert (response.provider, response.key_label, response.attempts) == ("gemini", "gemini#1", 2)
    assert len(clock.sleeps) == 1 and 2 <= clock.sleeps[0] <= 3


def test_long_cooldown_is_not_waited_out(fake, clock):
    fake.queue("gemini", G[0], [gemini_minute_429(retry_delay="600s")])
    router = make(fake, clock, gemini=1, LLM_PROVIDER_ORDER="gemini")

    with pytest.raises(LLMUnavailableError) as caught:
        ask(router)

    assert clock.sleeps == [] and caught.value.retry_at == pytest.approx(clock.now + 600, abs=70)


def test_total_attempts_are_capped(fake, clock):
    fake.always("gemini", gemini_error(503, "UNAVAILABLE", "overloaded"))
    fake.always("groq", openai_error(503, "overloaded"))
    fake.always("openrouter", openai_error(503, "overloaded"))
    router = make(fake, clock, LLM_MAX_TOTAL_ATTEMPTS="4")

    with pytest.raises(LLMUnavailableError):
        ask(router)

    assert len(fake.calls) == 4


def test_overall_deadline_is_respected(fake, clock):
    fake.always("gemini", gemini_error(503, "UNAVAILABLE", "overloaded"))
    router = make(fake, clock, LLM_OVERALL_DEADLINE_S="1", LLM_BACKOFF_BASE_S="5", LLM_BACKOFF_CAP_S="5")

    with pytest.raises(LLMUnavailableError):
        ask(router)

    assert clock.now - T0 < 1.5 and len(fake.calls) < 8


def test_circuit_breaker_pauses_a_failing_provider(fake, clock):
    fake.always("gemini", gemini_error(503, "UNAVAILABLE", "overloaded"))
    router = make(fake, clock)
    for _ in range(3):
        ask(router)  # three requests in a row end in a Gemini transient failure
    gemini_calls = len(fake.calls_to("gemini"))

    ask(router)  # breaker open: Gemini is skipped entirely

    assert len(fake.calls_to("gemini")) == gemini_calls
    clock.advance(31)
    ask(router)
    assert len(fake.calls_to("gemini")) > gemini_calls  # probed again after the pause


# ------------------------------------------------------------------ structured output
def test_structured_output_success_and_native_modes(fake, clock):
    fake.always("gemini", gemini_ok(f"```json\n{VALID_PLAN}\n```"))
    router = make(fake, clock)

    response = ask(router, response_model=Plan)

    assert response.parsed == Plan(title="T", steps=["a", "b"])
    assert fake.calls[0].payload["generationConfig"]["responseMimeType"] == "application/json"


def test_structured_validation_failure_gets_one_repair_attempt(fake, clock):
    fake.queue("gemini", G[0], [gemini_ok("sorry, here is prose"), gemini_ok(VALID_PLAN)])
    router = make(fake, clock)

    response = ask(router, response_model=Plan)

    assert response.parsed.title == "T" and response.attempts == 2 and response.provider == "gemini"
    repair = fake.calls[1].payload["contents"][-1]["parts"][0]["text"]
    assert "not valid" in repair
    assert state_of(router, "gemini#1")["status"] == "available"  # validation is not a key problem


def test_persistent_validation_failure_falls_back_with_provider_native_schema(fake, clock):
    fake.always("gemini", gemini_ok(json.dumps({"title": 5})))  # wrong type, missing steps
    fake.always("groq", openai_ok(VALID_PLAN))
    router = make(fake, clock)

    response = ask(router, response_model=Plan)

    assert response.provider == "groq" and response.parsed.steps == ["a", "b"]
    assert len(fake.calls_to("gemini", G[0])) == 2  # original + one repair, then moved on
    assert len(fake.calls_to("gemini", G[1])) == 0
    fmt = fake.calls_to("groq")[0].payload["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert sorted(fmt["json_schema"]["schema"]["required"]) == ["steps", "title"]


def test_openrouter_structured_request_requires_parameter_support(fake, clock):
    fake.always("openrouter", openai_ok(VALID_PLAN))
    router = make(fake, clock, LLM_PROVIDER_ORDER="openrouter")

    assert ask(router, response_model=Plan).parsed.title == "T"
    payload = fake.calls[0].payload
    assert (
        payload["provider"] == {"require_parameters": True}
        and payload["response_format"]["type"] == "json_schema"
    )


def test_structured_output_invalid_everywhere_is_reported_as_malformed(fake, clock):
    router = make(fake, clock)  # defaults reply with plain text, never JSON
    with pytest.raises(LLMUnavailableError) as caught:
        ask(router, response_model=Plan)
    assert caught.value.all_malformed


# ----------------------------------------------------------------------- concurrency
def test_concurrent_requests_respect_the_per_provider_limit(fake, clock):
    fake.always("gemini", Reply(200, gemini_ok().body, delay=0.05))
    router = make(fake, clock, LLM_MAX_CONCURRENCY="3")
    results, errors = [], []

    def worker():
        try:
            results.append(ask(router))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors and len(results) == 12
    assert fake.max_in_flight <= 3 and len(fake.calls) == 12


def test_concurrent_requests_share_key_health(fake, clock):
    fake.always("gemini", Reply(200, gemini_ok().body, delay=0.02))
    fake.queue("gemini", G[0], [gemini_daily_429()] * 8)  # key 1 is out of quota for everyone
    router = make(fake, clock)
    results = []

    threads = [threading.Thread(target=lambda: results.append(ask(router))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(results) == 8 and all(r.provider == "gemini" for r in results)
    assert state_of(router, "gemini#1")["status"] == "cooling_down"
    in_flight_when_it_failed = len(fake.calls_to("gemini", G[0]))  # threads already past the check may try it
    ask(router)
    assert len(fake.calls_to("gemini", G[0])) == in_flight_when_it_failed  # afterwards it is skipped


# ------------------------------------------------------------- secrets and accounting
def test_secrets_never_leak_into_errors_logs_or_snapshots(fake, clock, caplog):
    leaky = gemini_error(503, "UNAVAILABLE", f"upstream said key={G[0]} and Bearer {OR} were rejected")
    fake.always("gemini", leaky)
    fake.always("groq", openai_error(429, f"limit for {GROQ} reached (TPD)"))
    fake.always("openrouter", openai_error(402, f"credits gone for {OR}"))
    router = make(fake, clock)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LLMUnavailableError) as caught:
        ask(router)

    haystacks = [
        str(caught.value),
        *(f.message for f in caught.value.failures),
        json.dumps(router.keys.snapshot()),
        caplog.text,
        repr(router.keys.all_keys("gemini")),
    ]
    for text in haystacks:
        assert not any(secret in text for secret in ALL_KEYS), text[:200]


def test_usage_records_cover_every_attempt_without_secrets(fake, clock):
    records = []
    fake.queue("gemini", G[0], [gemini_minute_429()])
    router = make(fake, clock, usage_recorder=records.append)

    ask(router, task="planner")

    assert [(r.provider, r.key_label, r.outcome) for r in records] == [
        ("gemini", "gemini#1", "rate_limit"),
        ("gemini", "gemini#2", "ok"),
    ]
    assert records[1].prompt_tokens == 5 and records[0].http_status == 429
    assert all(r.task == "planner" for r in records)
    assert not any(secret in str(asdict(r)) for r in records for secret in ALL_KEYS)


def test_a_failing_usage_recorder_never_breaks_a_request(fake, clock):
    def broken(_record):
        raise RuntimeError("database down")

    router = make(fake, clock, usage_recorder=broken)
    assert ask(router).provider == "gemini"


def test_task_tiers_select_models(fake, clock):
    router = make(fake, clock, GEMINI_MODEL_QUALITY="quality-model", GEMINI_MODEL_FAST="fast-model")
    ask(router, task="planner")
    ask(router, task="researcher")
    assert [c.model for c in fake.calls] == ["quality-model", "fast-model"]


def test_probe_checks_each_key_and_updates_health(fake, clock):
    fake.queue("gemini", G[1], [gemini_invalid_key()])
    router = make(fake, clock, gemini=3)

    rows = router.probe("gemini")

    assert [(r["label"], r["ok"]) for r in rows] == [
        ("gemini#1", True),
        ("gemini#2", False),
        ("gemini#3", True),
    ]
    assert rows[1]["kind"] == "auth_invalid"
    assert state_of(router, "gemini#2")["status"] == "disabled"
    assert not any(secret in json.dumps(rows) for secret in ALL_KEYS)


def test_metrics_count_requests_and_fallbacks(fake, clock):
    fake.always("gemini", gemini_error(503, "UNAVAILABLE", "x"))
    router = make(fake, clock)
    ask(router)
    metrics = router.metrics()
    assert metrics["requests"] == 1 and metrics["succeeded"] == 1 and metrics["fallbacks"] == 1
