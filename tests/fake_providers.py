"""A scriptable fake of the Gemini, Groq and OpenRouter HTTP APIs.

Tests talk to it over real HTTP (through the real adapters and ``requests``), so timeouts,
headers, status codes and bodies are exercised end to end. Nothing here is a real provider.

Script responses per (provider, api key): ``fake.queue("gemini", KEY, [resp, resp, ...])``.
When a key's queue is empty the server answers with a successful completion. A response is
a :class:`Reply`; ``Reply.delay`` makes the server sleep first (to trigger client timeouts).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Reply:
    status: int = 200
    body: object = None  # dict/list -> JSON; str -> raw text
    headers: dict = field(default_factory=dict)
    delay: float = 0.0


@dataclass
class Call:
    provider: str
    key: str
    model: str
    payload: dict
    path: str
    headers: dict


# ------------------------------------------------------------------ body builders
def gemini_ok(text="ok", prompt=5, out=3):
    return Reply(200, {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": out, "totalTokenCount": prompt + out},
    })  # fmt: skip


def openai_ok(text="ok", prompt=5, out=3):
    return Reply(200, {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out},
    })  # fmt: skip


def gemini_error(status, gstatus, message, details=None, headers=None):
    return Reply(status, {"error": {"code": status, "message": message, "status": gstatus,
                                    "details": details or []}}, headers or {})  # fmt: skip


def gemini_minute_429(retry_delay=None):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]  # fmt: skip
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return gemini_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded for requests per minute", details)


def gemini_daily_429():
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]  # fmt: skip
    return gemini_error(
        429, "RESOURCE_EXHAUSTED", "You exceeded your current quota (requests per day)", details
    )


def gemini_ambiguous_429():
    """A 429 that says nothing about whether the limit is per minute or per day."""
    return gemini_error(429, "RESOURCE_EXHAUSTED", "Resource has been exhausted (e.g. check quota).")


def gemini_invalid_key():
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]
    return gemini_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", details)


def openai_error(status, message, code=None, headers=None):
    return Reply(status, {"error": {"message": message, "code": code, "type": "error"}}, headers or {})


# ------------------------------------------------------------------------- server
class FakeProviders:
    def __init__(self) -> None:
        self.queues: dict[tuple[str, str], list[Reply]] = {}
        self.defaults: dict[str, Reply] = {}
        self.calls: list[Call] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output quiet
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {}
                reply = fake._handle(self.path, dict(self.headers), payload)
                if reply.delay:
                    time.sleep(reply.delay)
                body = reply.body
                data = (body if isinstance(body, str) else json.dumps(body)).encode()
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for k, v in reply.headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):  # client timed out and left
                    pass
                finally:
                    with fake._lock:
                        fake.in_flight -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # lifecycle
    def start(self) -> FakeProviders:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def env(self, **extra) -> dict[str, str]:
        """Environment pointing every adapter at this server."""
        return {
            "GEMINI_BASE_URL": f"{self.base}/gemini",
            "GROQ_BASE_URL": f"{self.base}/groq",
            "OPENROUTER_BASE_URL": f"{self.base}/openrouter",
            **extra,
        }

    # scripting
    def queue(self, provider: str, key: str, replies: list[Reply]) -> None:
        self.queues.setdefault((provider, key), []).extend(replies)

    def always(self, provider: str, reply: Reply) -> None:
        """Every call to this provider, for any key, gets this reply (after queued ones)."""
        self.defaults[provider] = reply

    def calls_to(self, provider: str, key: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.provider == provider and (key is None or c.key == key)]

    # request handling
    def _handle(self, path: str, headers: dict, payload: dict) -> Reply:
        lowered = {k.lower(): v for k, v in headers.items()}
        if path.startswith("/gemini"):
            provider, key = "gemini", lowered.get("x-goog-api-key", "")
            model = path.split("/models/")[-1].split(":")[0]
        else:
            provider = "groq" if path.startswith("/groq") else "openrouter"
            key = lowered.get("authorization", "").removeprefix("Bearer ")
            model = payload.get("model", "")
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.calls.append(Call(provider, key, model, payload, path, lowered))
            queue = self.queues.get((provider, key))
            if queue:
                return queue.pop(0)
        default = self.defaults.get(provider)
        if default is not None:
            return default
        return gemini_ok(f"ok:{provider}") if provider == "gemini" else openai_ok(f"ok:{provider}")
