"""Model catalogue: which model each provider uses per tier, and what each can do.

Model identifiers live here and in environment variables only, never scattered through
the code. Defaults were checked against the providers' public documentation on
2026-10-13; model catalogues change often, so override them with ``<PROVIDER>_MODEL_QUALITY``
and ``<PROVIDER>_MODEL_FAST`` and verify with ``flask llm-ping``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from providers.base import LLMRequest

# Structured-output modes a model supports
STRICT_SCHEMA = "json_schema_strict"  # provider enforces the schema
JSON_OBJECT = "json_object"  # provider guarantees valid JSON only
PROMPT_ONLY = "prompt_only"  # no native JSON support; rely on the prompt


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    id: str
    tier: str = "quality"  # "quality" or "fast"
    structured: str = JSON_OBJECT
    context_window: int | None = None  # tokens; None = unknown (provider will tell us)
    max_output_tokens: int | None = None


# Capabilities for models we know about. Source: provider docs (Groq models page, Groq
# structured-outputs page, Gemini models page, OpenRouter models API) on 2026-10-13.
# Context/output limits are only recorded where the docs stated them.
KNOWN_MODELS: dict[tuple[str, str], ModelSpec] = {
    ("gemini", "gemini-3.8-flash"): ModelSpec("gemini", "gemini-3.8-flash"),
    ("gemini", "gemini-3.6-flash"): ModelSpec("gemini", "gemini-3.6-flash"),
    ("gemini", "gemini-3.5-flash-lite"): ModelSpec("gemini", "gemini-3.5-flash-lite", tier="fast"),
    ("gemini", "gemini-3.1-flash-lite"): ModelSpec("gemini", "gemini-3.1-flash-lite", tier="fast"),
    ("gemini", "gemini-2.5-flash"): ModelSpec("gemini", "gemini-2.5-flash"),
    ("gemini", "gemini-2.5-flash-lite"): ModelSpec("gemini", "gemini-2.5-flash-lite", tier="fast"),
    ("groq", "openai/gpt-oss-120b"): ModelSpec(
        "groq",
        "openai/gpt-oss-120b",
        structured=STRICT_SCHEMA,
        context_window=131072,
        max_output_tokens=65536,
    ),
    ("groq", "openai/gpt-oss-20b"): ModelSpec(
        "groq",
        "openai/gpt-oss-20b",
        tier="fast",
        structured=STRICT_SCHEMA,
        context_window=131072,
        max_output_tokens=65536,
    ),
    ("groq", "llama-3.3-70b-versatile"): ModelSpec(
        "groq",
        "llama-3.3-70b-versatile",
        structured=JSON_OBJECT,
        context_window=131072,
        max_output_tokens=32768,
    ),
    ("groq", "llama-3.1-8b-instant"): ModelSpec(
        "groq",
        "llama-3.1-8b-instant",
        tier="fast",
        structured=JSON_OBJECT,
        context_window=131072,
        max_output_tokens=131072,
    ),
    ("openrouter", "apodex/apodex-1.1-mini:free"): ModelSpec(
        "openrouter",
        "apodex/apodex-1.1-mini:free",
        structured=STRICT_SCHEMA,
        context_window=262144,
        max_output_tokens=235929,
    ),
}

DEFAULT_MODEL_IDS: dict[str, dict[str, str]] = {
    # "-latest" aliases follow Google's current model, so a retired name cannot break a deployment.
    # gemini-flash-lite-latest was confirmed with real keys; gemini-flash-latest was not. If it fails,
    # set GEMINI_MODEL_QUALITY in .env (list your models with check_keys.py or Google's models page).
    "gemini": {"quality": "gemini-flash-latest", "fast": "gemini-flash-lite-latest"},
    "groq": {"quality": "openai/gpt-oss-120b", "fast": "openai/gpt-oss-20b"},
    # Free OpenRouter models change often; this one answered with a real key on 2026-10-20.
    # Override with OPENROUTER_MODEL_QUALITY / _FAST when it disappears.
    "openrouter": {"quality": "minimax/minimax-m3.1-flash:free", "fast": "minimax/minimax-m3.1-flash:free"},
}

# Which tier each agent task uses. Unknown tasks use "quality".
DEFAULT_TASK_TIERS = {
    "planner": "quality",
    "researcher": "fast",
    "analyst": "quality",
    "writer": "quality",
    "critic": "quality",
    "validator": "fast",
    "ping": "fast",
}

# Unknown models get the safest structured mode for their provider.
_UNKNOWN_MODEL_MODE = {"gemini": JSON_OBJECT, "groq": JSON_OBJECT, "openrouter": PROMPT_ONLY}


def spec_for(provider: str, model_id: str, tier: str) -> ModelSpec:
    known = KNOWN_MODELS.get((provider, model_id))
    if known is not None:
        return replace(known, tier=tier)
    return ModelSpec(provider, model_id, tier=tier, structured=_UNKNOWN_MODEL_MODE.get(provider, PROMPT_ONLY))


def estimate_tokens(request: LLMRequest) -> int:
    """Cheap input-size estimate (about 3.5 characters per token), good enough to skip models
    whose context window is certainly too small."""
    chars = len(request.system or "") + sum(len(m.content) for m in request.messages)
    return int(chars / 3.5) + 1


class ModelCatalog:
    def __init__(self, specs: list[ModelSpec], task_tiers: dict[str, str] | None = None):
        self.specs = specs
        self.task_tiers = {**DEFAULT_TASK_TIERS, **(task_tiers or {})}

    @classmethod
    def from_env(cls, environ) -> ModelCatalog:
        specs: list[ModelSpec] = []
        for provider, tiers in DEFAULT_MODEL_IDS.items():
            for tier, default_id in tiers.items():
                env_name = f"{provider.upper()}_MODEL_{tier.upper()}"
                model_id = (environ.get(env_name) or "").strip() or default_id
                specs.append(spec_for(provider, model_id, tier))
        return cls(specs)

    def tier_for(self, request: LLMRequest) -> str:
        return request.tier or self.task_tiers.get(request.task, "quality")

    def models_for(self, provider: str) -> dict[str, ModelSpec]:
        return {s.tier: s for s in self.specs if s.provider == provider}

    def select(self, provider: str, request: LLMRequest) -> ModelSpec | None:
        """Pick this provider's model for the request, or None if none can take it."""
        tier = self.tier_for(request)
        candidates = [s for s in self.specs if s.provider == provider]
        candidates.sort(key=lambda s: s.tier != tier)  # preferred tier first
        needed = estimate_tokens(request)
        for spec in candidates:
            if spec.context_window is not None and needed > spec.context_window * 0.9:
                continue
            return spec
        return None
