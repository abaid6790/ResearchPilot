"""Opt-in checks against the REAL services. They use real API quota and the network.

Run with:   RUN_LIVE_TESTS=1 python -m pytest tests/live -m live -v
Keys come from your environment / .env (the normal variable names). A check is skipped when its
key is not configured. Nothing here is part of the routine suite, and nothing prints a key.
"""

from __future__ import annotations

import os

import pytest

from providers.models import LLMRequest
from providers.settings import PROVIDERS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to run live tests"),
]


@pytest.fixture()
def live_router(app):
    app.config["LLM_ENVIRON"] = dict(os.environ)
    app.config["LLM_RECORD_USAGE"] = False
    from providers import get_router

    return get_router(app)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_provider_keys_answer(live_router, provider):
    if provider not in live_router.active_providers():
        pytest.skip(f"no {provider} key configured")
    results = live_router.probe(provider)
    assert results, "no keys were probed"
    failures = [f"{r['label']}: {r.get('kind')} {r.get('message')}" for r in results if not r["ok"]]
    assert not failures, failures


def test_router_generates_text(live_router):
    if not live_router.active_providers():
        pytest.skip("no LLM key configured")
    request = LLMRequest.simple(
        "Reply with the single word: pong", tier="fast", task="live", max_output_tokens=64
    )
    response = live_router.generate(request)
    assert response.text.strip()


def test_tavily_search(app):
    if not os.getenv("TAVILY_API_KEY"):
        pytest.skip("TAVILY_API_KEY not set")
    from research_tools.search import TavilyClient

    client = TavilyClient.from_env(os.environ)
    response = client.search("what is retrieval augmented generation", max_results=3)
    assert response.hits and all(h.url.startswith("http") for h in response.hits)


def test_safe_fetcher_reads_a_public_page():
    from research_tools.fetch import SafeFetcher

    result = SafeFetcher().fetch("https://example.com/")
    assert result.ok and result.kind == "html" and b"Example Domain" in result.body


def test_safe_fetcher_refuses_internal_addresses():
    from research_tools.fetch import SafeFetcher

    result = SafeFetcher().fetch("http://169.254.169.254/latest/meta-data/")
    assert not result.ok and result.reason == "unsafe"
