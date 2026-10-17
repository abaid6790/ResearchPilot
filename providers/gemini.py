"""Google Gemini adapter (``models/{model}:generateContent`` REST endpoint).

Verified against Google's public docs on 2026-10-13:
* auth via the ``x-goog-api-key`` header (the key is never put in the URL);
* quotas are per *project*, not per key; daily quotas reset at midnight Pacific;
* 429 RESOURCE_EXHAUSTED and 503 UNAVAILABLE are retryable; 400/402/403 are not.

Not confirmed from the current docs (the quickstart now leads with a newer "Interactions"
API, while ``generateContent`` remains in the API reference with no deprecation notice): the
exact sub-fields of ``generationConfig`` and of 429 ``error.details``. The code below uses
the long-standing field names and tolerates missing or differently shaped error details;
``flask llm-ping`` checks them against the live API.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from providers.base import ErrorKind, LLMRequest, ProviderAdapter, ProviderError, RawResult, Usage
from providers.http import HttpResult, parse_duration, parse_retry_after, post_json, short
from providers.models import JSON_OBJECT, ModelSpec
from providers.structured import schema_instruction

BLOCKED_FINISH_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
_MODEL_ID = re.compile(r"^[A-Za-z0-9._\-]+$")
_TOKEN_LIMIT_HINTS = ("token count", "maximum number of tokens", "too long", "exceeds the maximum")


def _scope_from_quota_text(text: str) -> str | None:
    lowered = text.lower().replace("_", "").replace(" ", "")
    if "perday" in lowered or "daily" in lowered:
        return "day"
    if "perminute" in lowered:
        return "minute"
    return None


class GeminiAdapter(ProviderAdapter):
    name = "gemini"

    def __init__(self, base_url: str = "https://generativelanguage.googleapis.com"):
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------ request
    def build_payload(self, request: LLMRequest, model: ModelSpec) -> dict:
        system_parts = [request.system] if request.system else []
        if request.response_model is not None:
            system_parts.append(schema_instruction(request.response_model))
        payload: dict = {
            "contents": [
                {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
                for m in request.messages
            ]
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        config: dict = {}
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if request.max_output_tokens:
            limit = model.max_output_tokens or request.max_output_tokens
            config["maxOutputTokens"] = min(request.max_output_tokens, limit)
        if request.response_model is not None and model.structured == JSON_OBJECT:
            config["responseMimeType"] = "application/json"
        if config:
            payload["generationConfig"] = config
        return payload

    def generate(self, request: LLMRequest, *, api_key: str, model: ModelSpec, timeout: float) -> RawResult:
        if not _MODEL_ID.match(model.id):
            raise ProviderError(ErrorKind.BAD_REQUEST, "invalid model identifier", provider=self.name)
        url = f"{self.base_url}/v1beta/models/{quote(model.id, safe='')}:generateContent"
        result = post_json(
            self.name,
            url,
            headers={"x-goog-api-key": api_key},
            payload=self.build_payload(request, model),
            timeout=timeout,
            model=model.id,
        )
        if result.status != 200:
            raise self.error_from_response(result, model.id)
        return self.parse_success(result, model.id)

    # ----------------------------------------------------------------- response
    def parse_success(self, result: HttpResult, model_id: str) -> RawResult:
        body = result.body
        if not isinstance(body, dict):
            raise ProviderError(
                ErrorKind.MALFORMED_RESPONSE,
                "response was not a JSON object",
                provider=self.name,
                status=result.status,
                model=model_id,
            )
        feedback = body.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ProviderError(
                ErrorKind.CONTENT_BLOCKED,
                f"prompt blocked ({feedback['blockReason']})",
                provider=self.name,
                status=200,
                model=model_id,
            )
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ProviderError(
                ErrorKind.MALFORMED_RESPONSE,
                "response had no candidates",
                provider=self.name,
                status=200,
                model=model_id,
            )
        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish in BLOCKED_FINISH_REASONS:
            raise ProviderError(
                ErrorKind.CONTENT_BLOCKED,
                f"response blocked ({finish})",
                provider=self.name,
                status=200,
                model=model_id,
            )
        parts = (
            (candidate.get("content") or {}).get("parts")
            if isinstance(candidate.get("content"), dict)
            else None
        )
        text = "".join(
            p["text"]
            for p in (parts or [])
            if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought")
        )
        if not text.strip():
            raise ProviderError(
                ErrorKind.MALFORMED_RESPONSE,
                f"response had no text (finishReason={finish})",
                provider=self.name,
                status=200,
                model=model_id,
            )
        usage = body.get("usageMetadata") if isinstance(body.get("usageMetadata"), dict) else {}
        return RawResult(
            text=text,
            usage=Usage(
                usage.get("promptTokenCount"), usage.get("candidatesTokenCount"), usage.get("totalTokenCount")
            ),
            finish_reason=str(finish).lower() if finish else None,
        )

    def error_from_response(self, result: HttpResult, model_id: str) -> ProviderError:
        status, body = result.status, result.body
        error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        message = short(error.get("message") or (body if isinstance(body, str) else "") or f"HTTP {status}")
        gstatus = str(error.get("status") or "").upper()
        code = str(error.get("code") or "").lower()
        details = error.get("details") if isinstance(error.get("details"), list) else []

        retry_after = parse_retry_after(result.headers.get("retry-after"))
        scope = _scope_from_quota_text(message)
        reason = ""
        for detail in details:
            if not isinstance(detail, dict):
                continue
            kind = str(detail.get("@type", ""))
            if kind.endswith("RetryInfo") and isinstance(detail.get("retryDelay"), str):
                retry_after = retry_after or parse_duration(detail["retryDelay"])
            if kind.endswith("QuotaFailure"):
                for violation in detail.get("violations") or []:
                    if isinstance(violation, dict):
                        scope = (
                            _scope_from_quota_text(
                                f"{violation.get('quotaId', '')}{violation.get('quotaMetric', '')}"
                            )
                            or scope
                        )
            if kind.endswith("ErrorInfo"):
                reason = str(detail.get("reason") or "")  # e.g. API_KEY_INVALID

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

        lowered = message.lower()
        if (
            status == 429
            or gstatus == "RESOURCE_EXHAUSTED"
            or code in ("rate_limit_exceeded", "too_many_requests")
        ):
            return (
                make(ErrorKind.QUOTA_EXHAUSTED, "day")
                if scope == "day"
                else make(ErrorKind.RATE_LIMIT, scope)
            )
        if code == "quota_exceeded":
            return make(ErrorKind.QUOTA_EXHAUSTED, "day")
        if status == 402 or code == "payment_required":
            return make(ErrorKind.QUOTA_EXHAUSTED, "credits")
        if (
            status in (401, 403)
            or gstatus in ("UNAUTHENTICATED", "PERMISSION_DENIED")
            or reason == "API_KEY_INVALID"
            or "api key not valid" in lowered
            or "api key expired" in lowered
        ):
            return make(ErrorKind.AUTH_INVALID)
        if status == 400:
            if any(hint in lowered for hint in _TOKEN_LIMIT_HINTS):
                return make(ErrorKind.CONTEXT_TOO_LONG)
            if gstatus == "FAILED_PRECONDITION":
                return make(ErrorKind.UNSUPPORTED)
            return make(ErrorKind.BAD_REQUEST)
        if status == 404:
            return make(ErrorKind.UNSUPPORTED)
        if status in (408, 529) or status >= 500:
            return make(ErrorKind.TRANSIENT)
        if 300 <= status < 400:
            return make(ErrorKind.MALFORMED_RESPONSE)
        return make(ErrorKind.BAD_REQUEST)
