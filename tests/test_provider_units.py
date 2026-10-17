"""Unit tests: settings, error classification, structured helpers, key manager, Redis store,
status endpoint and CLI. No network."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from providers import build_router, get_router
from providers.base import ErrorKind, StructuredOutputError
from providers.gemini import GeminiAdapter
from providers.groq import GroqAdapter
from providers.http import HttpResult, parse_duration, parse_retry_after
from providers.key_manager import (
    COOLING_DOWN,
    Cooldown,
    KeyInfo,
    KeyManager,
    MemoryHealthStore,
    RedisHealthStore,
    next_pacific_midnight,
)
from providers.openrouter import OpenRouterAdapter
from providers.settings import ProviderSettings, SettingsError
from providers.structured import extract_json, parse_structured, to_strict_schema
from tests.conftest import PASSWORD, login, make_user  # noqa: F401
from tests.test_providers import ALL_KEYS, FakeClock, G


# --------------------------------------------------------------------------- settings
def test_settings_defaults_and_key_discovery():
    s = ProviderSettings.from_env(
        {"GEMINI_API_KEY_1": "a" * 12, "GEMINI_API_KEY_3": "b" * 12, "GROQ_API_KEY_1": "c" * 12}
    )
    assert s.provider_order == ["gemini", "groq", "openrouter"]
    assert [k.label for k in s.keys] == ["gemini#1", "gemini#3", "groq#1"]
    assert s.key_counts() == {"gemini": 2, "groq": 1, "openrouter": 0}
    assert "a" * 12 not in repr(s)  # secrets are hidden from repr


def test_settings_ignore_blank_placeholder_and_duplicate_keys():
    s = ProviderSettings.from_env(
        {"GEMINI_API_KEY_1": "", "GEMINI_API_KEY_2": "changeme", "GEMINI_API_KEY_3": "real-key-1",
         "GEMINI_API_KEY_4": "real-key-1"}
    )  # fmt: skip
    assert [k.label for k in s.keys] == ["gemini#3"]


def test_settings_custom_order_and_project_groups():
    s = ProviderSettings.from_env(
        {"LLM_PROVIDER_ORDER": " Groq , gemini,groq", "GEMINI_API_KEY_1": "k1xxxxxxxx", "GEMINI_API_KEY_2": "k2xxxxxxxx",
         "GEMINI_API_KEY_1_PROJECT": "p", "GEMINI_API_KEY_2_PROJECT": "p"}
    )  # fmt: skip
    assert s.provider_order == ["groq", "gemini"]
    assert s.keys[0].group == s.keys[1].group == "p"


@pytest.mark.parametrize(
    "env",
    [
        {"LLM_PROVIDER_ORDER": "gemini,openai"},
        {"LLM_PROVIDER_ORDER": " , "},
        {"LLM_HEALTH_STORE": "mongo"},
        {"LLM_REQUEST_TIMEOUT_S": "fast"},
        {"LLM_MAX_TOTAL_ATTEMPTS": "-1"},
    ],
)
def test_invalid_settings_fail_clearly(env):
    with pytest.raises(SettingsError):
        ProviderSettings.from_env(env)


# ------------------------------------------------------------ error classification
def result(status, body, headers=None):
    return HttpResult(status, headers or {}, body, True)


GEMINI = GeminiAdapter()
GROQ_A = GroqAdapter()
OPENROUTER = OpenRouterAdapter()


@pytest.mark.parametrize(
    ("adapter", "res", "kind", "scope"),
    [
        (
            GEMINI,
            result(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "slow"}}),
            ErrorKind.RATE_LIMIT,
            None,
        ),
        (
            GEMINI,
            result(
                429,
                {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded: requests per day"}},
            ),
            ErrorKind.QUOTA_EXHAUSTED,
            "day",
        ),
        (GEMINI, result(429, "<html>too many</html>"), ErrorKind.RATE_LIMIT, None),
        (
            GEMINI,
            result(400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid"}}),
            ErrorKind.AUTH_INVALID,
            None,
        ),
        (
            GEMINI,
            result(403, {"error": {"status": "PERMISSION_DENIED", "message": "denied"}}),
            ErrorKind.AUTH_INVALID,
            None,
        ),
        (
            GEMINI,
            result(400, {"error": {"status": "INVALID_ARGUMENT", "message": "bad field"}}),
            ErrorKind.BAD_REQUEST,
            None,
        ),
        (GEMINI, result(404, {"error": {"message": "model not found"}}), ErrorKind.UNSUPPORTED, None),
        (GEMINI, result(500, {"error": {"message": "internal"}}), ErrorKind.TRANSIENT, None),
        (GEMINI, result(503, {"error": {"message": "overloaded"}}), ErrorKind.TRANSIENT, None),
        (GEMINI, result(504, "gateway timeout"), ErrorKind.TRANSIENT, None),
        (
            GROQ_A,
            result(429, {"error": {"message": "Rate limit reached ... tokens per minute (TPM)"}}),
            ErrorKind.RATE_LIMIT,
            "minute",
        ),
        (
            GROQ_A,
            result(429, {"error": {"message": "Rate limit reached ... requests per day (RPD)"}}),
            ErrorKind.QUOTA_EXHAUSTED,
            "day",
        ),
        (GROQ_A, result(401, {"error": {"message": "Invalid API Key"}}), ErrorKind.AUTH_INVALID, None),
        (GROQ_A, result(413, {"error": {"message": "Request too large"}}), ErrorKind.CONTEXT_TOO_LONG, None),
        (
            GROQ_A,
            result(400, {"error": {"message": "x", "code": "json_validate_failed"}}),
            ErrorKind.MALFORMED_RESPONSE,
            None,
        ),
        (GROQ_A, result(498, {"error": {"message": "flex capacity exceeded"}}), ErrorKind.BAD_REQUEST, None),
        (OPENROUTER, result(402, {"error": {"message": "credits"}}), ErrorKind.QUOTA_EXHAUSTED, "credits"),
        (
            OPENROUTER,
            result(403, {"error": {"message": "flagged", "metadata": {"reasons": ["x"]}}}),
            ErrorKind.CONTENT_BLOCKED,
            None,
        ),
        (
            OPENROUTER,
            result(403, {"error": {"message": "limit", "metadata": {"limit_source": "key"}}}),
            ErrorKind.QUOTA_EXHAUSTED,
            "credits",
        ),
        (
            OPENROUTER,
            result(429, {"error": {"message": "free-models-per-day limit"}}),
            ErrorKind.QUOTA_EXHAUSTED,
            "day",
        ),
        (OPENROUTER, result(502, {"error": {"message": "provider down"}}), ErrorKind.TRANSIENT, None),
        (OPENROUTER, result(408, {"error": {"message": "timeout"}}), ErrorKind.TRANSIENT, None),
    ],
)
def test_error_classification(adapter, res, kind, scope):
    error = adapter.error_from_response(res, "m")
    assert error.kind is kind and error.quota_scope == scope


def test_retry_hints_are_parsed():
    assert (
        parse_duration("34s") == 34 and parse_duration("1m2.5s") == 62.5 and parse_duration("120ms") == 0.12
    )
    assert parse_duration("nonsense") is None
    assert (
        parse_retry_after("17") == 17 and parse_retry_after("-5") == 0 and parse_retry_after("soon") is None
    )
    assert parse_retry_after(None) is None
    error = GROQ_A.error_from_response(
        result(429, {"error": {"message": "limit. Please try again in 2m59.5s."}}), "m"
    )
    assert error.retry_after == pytest.approx(179.5)
    error = GEMINI.error_from_response(
        result(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "x",
                               "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}), "m")  # fmt: skip
    assert error.retry_after == 34


def test_gemini_rejects_unsafe_model_ids():
    from providers.base import LLMRequest, ProviderError
    from providers.models import spec_for

    with pytest.raises(ProviderError):
        GEMINI.generate(
            LLMRequest.simple("x"), api_key="k", model=spec_for("gemini", "../admin?x=1", "fast"), timeout=1
        )


# --------------------------------------------------------------------- structured
class Item(BaseModel):
    name: str
    count: int = 1
    tags: list[str] | None = None


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go: {"a": [1, 2]} Hope that helps.') == {"a": [1, 2]}
    with pytest.raises(StructuredOutputError):
        extract_json("no json here")


def test_parse_structured_reports_where_but_not_values():
    assert parse_structured('{"name": "x"}', Item).count == 1
    with pytest.raises(StructuredOutputError) as caught:
        parse_structured('{"name": "SECRET-VALUE", "count": "many"}', Item)
    assert "count" in str(caught.value) and "SECRET-VALUE" not in str(caught.value)


def test_to_strict_schema_closes_objects_and_drops_unsupported_keywords():
    class Nested(BaseModel):
        inner: Item
        items: list[Item]
        score: float = 0.5

    schema = to_strict_schema(Nested.model_json_schema())
    assert schema["additionalProperties"] is False and set(schema["required"]) == {"inner", "items", "score"}
    item_def = schema["$defs"]["Item"]
    assert item_def["additionalProperties"] is False and set(item_def["required"]) == {
        "name",
        "count",
        "tags",
    }
    assert "default" not in json.dumps(schema)
    assert "default" in json.dumps(Nested.model_json_schema())  # original untouched


# ----------------------------------------------------------------------- key manager
def test_next_pacific_midnight_is_in_pacific_time():
    now = datetime(2027, 1, 10, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    reset = datetime.fromtimestamp(next_pacific_midnight(now), ZoneInfo("America/Los_Angeles"))
    assert (reset.year, reset.month, reset.day, reset.hour) == (2027, 1, 11, 0) and reset.minute <= 1


def test_next_pacific_midnight_across_dst_change():
    zone = ZoneInfo("America/Los_Angeles")
    now = datetime(2027, 3, 13, 20, 0, tzinfo=zone).timestamp()  # day before US spring-forward
    reset = datetime.fromtimestamp(next_pacific_midnight(now), zone)
    assert (reset.month, reset.day, reset.hour) == (3, 14, 0)


def test_memory_store_never_shortens_a_cooldown():
    store = MemoryHealthStore()
    store.put("k", Cooldown(200, "quota_exhausted"), 100)
    store.put("k", Cooldown(150, "rate_limit"), 100)
    assert store.get("k", 100).until == 200
    assert store.get("k", 201) is None


def manager(n=2, clock=None):
    keys = [KeyInfo("gemini", f"gemini#{i}", f"secret-{i}-xxxxxxxx", f"g{i}") for i in range(1, n + 1)]
    return KeyManager(keys, clock=clock or FakeClock()), keys


def test_key_manager_sticky_order_and_wraparound():
    km, keys = manager(3)
    assert [k.label for k in km.candidates("gemini")] == ["gemini#1", "gemini#2", "gemini#3"]
    km.report_success(keys[1])
    assert [k.label for k in km.candidates("gemini")] == ["gemini#2", "gemini#3", "gemini#1"]
    km.report_failure(keys[1], ErrorKind.RATE_LIMIT, retry_after=10)
    assert [k.label for k in km.candidates("gemini")] == ["gemini#3", "gemini#1"]


def test_key_manager_snapshot_has_no_secrets_and_reset_works():
    clock = FakeClock()
    km, keys = manager(2, clock)
    km.report_failure(keys[0], ErrorKind.AUTH_INVALID)
    snapshot = json.dumps(km.snapshot())
    assert "secret-" not in snapshot and "disabled" in snapshot
    assert km.reset("gemini#1") == 1 and km.is_available(keys[0])


def test_cooldown_ends_exactly_when_it_should():
    clock = FakeClock()
    km, keys = manager(1, clock)
    km.report_failure(keys[0], ErrorKind.RATE_LIMIT, retry_after=60)
    clock.advance(59)
    assert km.status_of(keys[0])[0] == COOLING_DOWN
    clock.advance(10)  # jitter is at most +10%
    assert km.is_available(keys[0])


# ------------------------------------------------------------------------ redis store
class FakeRedis:
    """Just enough of redis-py for RedisHealthStore."""

    def __init__(self, fail=False):
        self.data, self.fail = {}, fail

    def get(self, key):
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    def set(self, key, value, px=None):
        if self.fail:
            raise ConnectionError("down")
        self.data[key] = value

    def delete(self, key):
        if self.fail:
            raise ConnectionError("down")
        self.data.pop(key, None)


def test_redis_store_shares_cooldowns_between_managers():
    client = FakeRedis()
    clock = FakeClock()
    keys = [KeyInfo("gemini", "gemini#1", "s" * 12, "g1")]
    worker_a = KeyManager(keys, RedisHealthStore("", client), clock=clock)
    worker_b = KeyManager(keys, RedisHealthStore("", client), clock=clock)  # another process
    worker_a.report_failure(keys[0], ErrorKind.QUOTA_EXHAUSTED, scope="day")
    assert not worker_b.is_available(keys[0])
    worker_a.report_success(keys[0])
    assert worker_b.is_available(keys[0])


def test_redis_store_failure_degrades_to_no_shared_state(caplog):
    store = RedisHealthStore("", FakeRedis(fail=True))
    store.put("k", Cooldown(10**12, "rate_limit"), 0)
    assert store.get("k", 0) is None
    store.clear("k")
    assert "unavailable" in caplog.text


# ------------------------------------------------------- status endpoint and CLI
@pytest.fixture()
def llm_app(app, tmp_path):
    app.config["LLM_ENVIRON"] = {"GEMINI_API_KEY_1": G[0], "GROQ_API_KEY_1": ALL_KEYS[4]}
    app.extensions.pop("llm_router", None)
    return app


def test_status_endpoint_requires_login(client, llm_app):
    response = client.get("/health/providers")
    assert response.status_code in (302, 401)


def test_status_endpoint_shows_health_but_no_secrets(client, llm_app):
    make_user()
    login(client)
    response = client.get("/health/providers")
    assert response.status_code == 200
    data = response.get_json()
    assert data["active_providers"] == ["gemini", "groq"]
    assert {k["label"] for k in data["keys"]} == {"gemini#1", "groq#1"}
    assert not any(secret in response.get_data(as_text=True) for secret in ALL_KEYS)


def test_cli_status_and_reset(llm_app):
    runner = llm_app.test_cli_runner()
    out = runner.invoke(args=["llm-status"]).output
    assert "gemini -> groq" in out and "gemini#1" in out and not any(s in out for s in ALL_KEYS)
    get_router(llm_app).keys.report_failure(
        get_router(llm_app).keys.all_keys("gemini")[0], ErrorKind.RATE_LIMIT
    )
    assert "cooling_down" in runner.invoke(args=["llm-status"]).output
    assert "Reset 1 key" in runner.invoke(args=["llm-reset", "--key", "gemini#1"]).output


def test_cli_status_without_keys(app):
    app.config["LLM_ENVIRON"] = {}
    app.extensions.pop("llm_router", None)
    assert "No LLM API keys" in app.test_cli_runner().invoke(args=["llm-status"]).output


def test_cli_ping_reports_failures_with_exit_code(app):
    from tests.fake_providers import FakeProviders, gemini_invalid_key

    fake = FakeProviders().start()
    try:
        fake.always("gemini", gemini_invalid_key())
        app.config["LLM_ENVIRON"] = fake.env(GEMINI_API_KEY_1=G[0])
        app.extensions.pop("llm_router", None)
        result_ = app.test_cli_runner().invoke(args=["llm-ping", "--provider", "gemini"])
        assert result_.exit_code == 1 and "FAIL" in result_.output and "auth_invalid" in result_.output
        assert G[0] not in result_.output
    finally:
        fake.stop()


def test_build_router_is_isolated_from_process_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_1", "should-not-be-used-xxxxxxxx")
    assert build_router({}).active_providers() == []
