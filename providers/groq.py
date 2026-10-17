"""Groq adapter (OpenAI-compatible ``/openai/v1/chat/completions``).

Verified against Groq's public docs on 2026-10-13:
* limits apply per *organization*; exceeding one returns 429 with a ``retry-after`` header
  (seconds). The docs do not describe the message wording, so the daily-vs-minute
  distinction also uses the "(TPD)/(RPD)/(TPM)/(RPM)" wording seen in Groq error messages
  and degrades safely to a temporary rate limit when unsure;
* ``json_schema`` with ``strict: true`` works only on a few models (see ``providers/models.py``),
  ``json_object`` works on all; neither combines with streaming (not used here).
"""

from __future__ import annotations

from providers.openai_compat import OpenAICompatAdapter


class GroqAdapter(OpenAICompatAdapter):
    name = "groq"
    default_base_url = "https://api.groq.com/openai/v1"
    max_tokens_field = "max_completion_tokens"
