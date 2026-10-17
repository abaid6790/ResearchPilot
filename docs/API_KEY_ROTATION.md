# API key rotation and provider fallback

This describes exactly what `providers/` does when a call fails, what is verified, and what is
not. Code: `providers/router.py` (decisions), `providers/key_manager.py` (key health),
`providers/gemini.py`, `groq.py`, `openrouter.py` (error classification).

## What is and is not guaranteed

* More keys can help when a limit is **per key** or when a key is rejected. They do **not**
  create more quota when the limit is **per project or organization**:
  * **Gemini**: quotas are per Google Cloud *project*, not per key. Four keys created in the
    same project share one quota. Keys only add capacity when they belong to different
    projects. Declare shared projects with `GEMINI_API_KEY_n_PROJECT=<label>`; without it each
    key is assumed to be its own project.
  * **Groq**: limits are per *organization*. Extra keys from one organization do not add quota.
  * **OpenRouter**: free models are limited per account (20 requests/minute, and 50/day or
    1000/day with at least 10 credits purchased); extra keys on one account do not raise that.
* Using multiple accounts to get around a provider's limits may breach that provider's terms.
  Check them before relying on this.
* Nothing here makes a failing provider succeed. When every provider is unavailable the caller
  gets an honest `LLMUnavailableError` listing what failed; it never receives invented text.

## Decision table

| Failure (`ErrorKind`) | Typical cause | Action |
|---|---|---|
| `RATE_LIMIT` | HTTP 429 per-minute limit, or a 429 that does not say "per day" | Cool **this key** (provider's retry hint, else 30 s, doubling on repeats up to 1 h); try the provider's next key |
| `QUOTA_EXHAUSTED` | 429 that says per-day, or 402 / budget exhausted | Cool the key (Gemini: until midnight Pacific; others: provider hint or 1 h; credits: 30 min); keys in the same quota group are cooled too; next key |
| `AUTH_INVALID` | 401/403, "API key not valid" | Disable the key for 6 h (then it may be rechecked); next key |
| `TRANSIENT` | timeout, network error, 5xx, 408 | Retry the **same key** with backoff (max 2 extra tries); then go to the next **provider**. Keys are not cooled |
| `MALFORMED_RESPONSE` | empty/garbled body, no candidates | Same as `TRANSIENT`, but never counted against the provider's circuit breaker |
| `CONTEXT_TOO_LONG` | input exceeds the model window | Next provider (other keys have the same limit) |
| `CONTENT_BLOCKED` | safety/moderation refusal | Next provider |
| `UNSUPPORTED` | model or feature not available (404, precondition) | Next provider |
| `BAD_REQUEST` | our request is invalid | `LLMRequestError` immediately; no key or provider can fix it |

Key rotation happens **only** for the first three rows, so a flaky network or a bad prompt can
never burn through the keys or mark healthy keys as dead. A 429 is not assumed to be daily
quota: it is treated as a temporary rate limit unless the response says "per day" (Gemini also
reads `quotaId` in the error details).

Structured output: the reply is parsed and checked against the Pydantic model. If it does not
match, the model is asked once to correct itself (`LLM_MAX_VALIDATION_RETRIES`); if that also
fails, the router moves to the next provider. Validation failures never cool a key.

## Bounds (nothing can loop forever)

* at most `LLM_MAX_TOTAL_ATTEMPTS` (8) provider calls per request;
* at most `LLM_MAX_RETRIES_PER_KEY` (2) retries per key, with jittered exponential backoff
  (0.5 s base, 8 s cap);
* at most two passes over the providers; a second pass happens only if a cooling key returns
  within `LLM_MAX_INLINE_WAIT_S` (5 s) and the deadline allows it. A longer wait is never slept
  inside a request: the caller gets `LLMUnavailableError.retry_at` (epoch seconds) instead;
* `LLM_OVERALL_DEADLINE_S` (180 s) ceiling, and per-call timeouts;
* three consecutive transient failures pause a provider for 30 s (circuit breaker);
* at most `LLM_MAX_CONCURRENCY` (8) in-flight calls per provider per process.

## Key health storage

The default store is in memory: a process restart treats every key as healthy and learns again
from the provider's responses (one extra failed call per bad key). With `LLM_HEALTH_STORE=redis`
and `REDIS_URL`, cooldowns are shared by Gunicorn and Celery processes, so they do not each
rediscover an exhausted key. If Redis is unreachable the code logs a warning and behaves as
if there were no shared state; it does not fail requests.

A successful call clears the key's cooldown, even if a daily-quota cooldown was recorded
earlier (a late success proves the key works).

## Operating it

```powershell
flask llm-status                      # keys, models, cooldowns; no network
flask llm-ping                        # one tiny real call per key; uses a little quota
flask llm-ping --provider gemini --key gemini#2
flask llm-reset                       # forget cooldowns
```

`GET /health/providers` (signed-in users) returns the same status as JSON. Keys appear only as
labels such as `gemini#2`. Secrets are never in logs, exceptions, responses or `app.config`.

## Verification status

Verified by automated tests (45 + 49, `tests/test_providers.py`, `tests/test_provider_units.py`)
that run the real adapters over HTTP against a scripted local fake server: key 1 rate-limited
then key 2 succeeds; all Gemini keys exhausted; Gemini to Groq and Groq to OpenRouter fallback;
all providers unavailable; invalid credentials; timeouts; malformed responses; structured-output
repair and fallback; concurrency limit; retry caps, cooldown escalation and Retry-After; secret
redaction. The Redis store was also checked by hand against a real `redis-server`.

**Not verified:** behaviour against the live Gemini, Groq and OpenRouter services, because no
API keys were available when this was built. The fake server follows the providers' public
documentation, but:

* the exact shape of Gemini's 429 `details` and the wording of Groq's rate-limit messages are not
  fully documented; the classifier tolerates several shapes and degrades to "temporary rate
  limit" when unsure, which is the safe direction;
* Gemini's `generateContent` request fields (`generationConfig`, `responseMimeType`) follow the
  long-standing REST reference; Google's newer docs lead with a different API;
* default model IDs change often. Run `flask llm-ping` with your keys first; override with
  `<PROVIDER>_MODEL_QUALITY` / `_FAST` if a model is rejected.

Other limits: `AUTH_INVALID` for Gemini is detected from status 400/401/403 and the message
"API key not valid"; an unusual wording could be seen as a bad request. Token counts used for
the context check are estimates (about 3.5 characters per token).
