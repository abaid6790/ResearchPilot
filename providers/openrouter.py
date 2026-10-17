"""OpenRouter adapter (OpenAI-compatible ``/api/v1/chat/completions``).

Verified against OpenRouter's public docs on 2026-10-13:
* 401 invalid key, 402 insufficient credits, 403 permission/guardrail/moderation, 408 timeout,
  429 rate limited (may include ``Retry-After``), 502 model down, 503 no provider matches;
* once a provider accepts a request, failures can arrive as HTTP 200 with an ``error`` object
  and no ``choices``; the shared parser handles that;
* free models (ids ending ``:free``) allow 20 requests/minute, and 50/day (or 1000/day with at
  least 10 credits purchased); extra keys or accounts do not raise the limit;
* structured outputs need ``provider.require_parameters = true`` so requests are only routed
  to providers that support ``response_format``.
"""

from __future__ import annotations

from providers.base import ErrorKind, LLMRequest
from providers.models import ModelSpec
from providers.openai_compat import OpenAICompatAdapter


class OpenRouterAdapter(OpenAICompatAdapter):
    name = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
    max_tokens_field = "max_tokens"

    def extra_payload(self, payload: dict, request: LLMRequest, model: ModelSpec) -> None:
        if "response_format" in payload:
            payload["provider"] = {"require_parameters": True}

    def classify_special(self, status: int, error: dict, message: str) -> ErrorKind | None:
        metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
        if status == 403:
            if "reasons" in metadata or "flagged_input" in metadata:
                return ErrorKind.CONTENT_BLOCKED  # moderation flag
            if "limit_source" in metadata:
                return ErrorKind.QUOTA_EXHAUSTED  # spend/budget limit
        return None
