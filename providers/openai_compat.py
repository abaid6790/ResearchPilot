"""Shared logic for OpenAI-compatible ``/chat/completions`` APIs (Groq, OpenRouter)."""

from __future__ import annotations

import re

from providers.base import ErrorKind, LLMRequest, ProviderAdapter, ProviderError, RawResult, Usage
from providers.http import HttpResult, parse_duration, parse_retry_after, post_json, short
from providers.models import JSON_OBJECT, STRICT_SCHEMA, ModelSpec
from providers.structured import schema_instruction, to_strict_schema

_TRY_AGAIN = re.compile(r"try again in ([0-9hms.\s]+)", re.IGNORECASE)
_DAILY_HINTS = ("per day", "(tpd)", "(rpd)", "daily", "free-models-per-day")
_MINUTE_HINTS = ("per minute", "(tpm)", "(rpm)")
_CONTEXT_HINTS = ("context_length_exceeded", "maximum context length", "reduce the length", "too many tokens")


class OpenAICompatAdapter(ProviderAdapter):
    max_tokens_field = "max_tokens"
    default_base_url = ""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or self.default_base_url).rstrip("/")

    # ------------------------------------------------------------------ request
    def extra_headers(self) -> dict[str, str]:
        return {}

    def extra_payload(self, payload: dict, request: LLMRequest, model: ModelSpec) -> None:
        """Hook for provider-specific fields."""

    def build_payload(self, request: LLMRequest, model: ModelSpec) -> dict:
        system_parts = [request.system] if request.system else []
        response_format = None
        if request.response_model is not None:
            if model.structured == STRICT_SCHEMA:
                name = re.sub(r"[^A-Za-z0-9_-]", "_", request.response_model.__name__)[:64]
                response_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": name,
                        "strict": True,
                        "schema": to_strict_schema(request.response_model.model_json_schema()),
                    },
                }
            else:
                if model.structured == JSON_OBJECT:
                    response_format = {"type": "json_object"}
                system_parts.append(schema_instruction(request.response_model))
        messages = [{"role": "system", "content": "\n\n".join(system_parts)}] if system_parts else []
        messages += [{"role": m.role, "content": m.content} for m in request.messages]

        payload: dict = {"model": model.id, "messages": messages}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens:
            limit = model.max_output_tokens or request.max_output_tokens
            payload[self.max_tokens_field] = min(request.max_output_tokens, limit)
        if response_format is not None:
            payload["response_format"] = response_format
        self.extra_payload(payload, request, model)
        return payload

    def generate(self, request: LLMRequest, *, api_key: str, model: ModelSpec, timeout: float) -> RawResult:
        result = post_json(
            self.name,
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", **self.extra_headers()},
            payload=self.build_payload(request, model),
            timeout=timeout,
            model=model.id,
        )
        body = result.body
        # OpenRouter can report failures inside an HTTP 200 body.
        if result.status == 200 and isinstance(body, dict) and body.get("error") and not body.get("choices"):
            embedded = body["error"] if isinstance(body["error"], dict) else {}
            code = embedded.get("code")
            result = HttpResult(code if isinstance(code, int) else 502, result.headers, body, result.is_json)
        if result.status != 200:
            raise self.error_from_response(result, model.id)
        return self.parse_success(result, model.id)

    # ----------------------------------------------------------------- response
    def parse_success(self, result: HttpResult, model_id: str) -> RawResult:
        body = result.body

        def bad(kind: ErrorKind, message: str) -> ProviderError:
            return ProviderError(kind, message, provider=self.name, status=200, model=model_id)

        if not isinstance(body, dict):
            raise bad(ErrorKind.MALFORMED_RESPONSE, "response was not a JSON object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise bad(ErrorKind.MALFORMED_RESPONSE, "response had no choices")
        choice = choices[0]
        finish = choice.get("finish_reason")
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, list):  # some providers return typed parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if finish == "content_filter":
            raise bad(ErrorKind.CONTENT_BLOCKED, "response blocked by content filter")
        if not isinstance(content, str) or not content.strip():
            raise bad(ErrorKind.MALFORMED_RESPONSE, f"response had no text (finish_reason={finish})")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return RawResult(
            text=content,
            usage=Usage(
                usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")
            ),
            finish_reason=str(finish) if finish else None,
        )

    def classify_special(self, status: int, error: dict, message: str) -> ErrorKind | None:
        """Provider-specific classification; return None to fall through to the shared rules."""
        return None

    def error_from_response(self, result: HttpResult, model_id: str) -> ProviderError:
        status, body = result.status, result.body
        error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        message = short(error.get("message") or (body if isinstance(body, str) else "") or f"HTTP {status}")
        lowered = message.lower()

        retry_after = parse_retry_after(result.headers.get("retry-after"))
        if retry_after is None:
            hint = _TRY_AGAIN.search(message)
            retry_after = parse_duration(hint.group(1)) if hint else None
        scope = (
            "day"
            if any(h in lowered for h in _DAILY_HINTS)
            else "minute"
            if any(h in lowered for h in _MINUTE_HINTS)
            else None
        )

        def make(kind: ErrorKind, scope_value: str | None = None) -> ProviderError:
            return ProviderError(
                kind,
                message,
                provider=self.name,
                status=status,
                retry_after=retry_after,
                quota_scope=scope_value,
                model=model_id,
            )

        special = self.classify_special(status, error, message)
        if special is not None:
            return make(special, "credits" if special is ErrorKind.QUOTA_EXHAUSTED else None)
        if status == 429:
            return (
                make(ErrorKind.QUOTA_EXHAUSTED, "day")
                if scope == "day"
                else make(ErrorKind.RATE_LIMIT, scope)
            )
        if status == 402:
            return make(ErrorKind.QUOTA_EXHAUSTED, "credits")
        if status in (401, 403):
            return make(ErrorKind.AUTH_INVALID)
        if status == 413 or (status == 400 and any(h in lowered for h in _CONTEXT_HINTS)):
            return make(ErrorKind.CONTEXT_TOO_LONG)
        if status == 400:
            if str(error.get("code") or "") == "json_validate_failed":
                return make(ErrorKind.MALFORMED_RESPONSE)
            return make(ErrorKind.BAD_REQUEST)
        if status == 404:
            return make(ErrorKind.UNSUPPORTED)
        if status in (408, 529) or status >= 500:
            return make(ErrorKind.TRANSIENT)
        if 300 <= status < 400:
            return make(ErrorKind.MALFORMED_RESPONSE)
        return make(ErrorKind.BAD_REQUEST)
