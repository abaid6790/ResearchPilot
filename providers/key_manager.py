"""API key health registry: which key is usable right now, and when each cooling key returns.

Design points (see docs/API_KEY_ROTATION.md):

* Keys are identified by a non-secret label such as ``gemini#2``; secrets never appear in
  snapshots, logs or exceptions.
* A 429 is *not* assumed to mean "daily quota gone". Each failure is classified by the
  adapter (rate limit vs quota vs invalid key) and gets a matching cooldown.
* Cooldowns live in a pluggable store. The default is in-memory (a restart starts every
  key healthy and re-learns from the provider); a Redis store shares cooldowns between
  Gunicorn/Celery processes so they do not all hammer a limited key.
* Repeated failures escalate (exponential growth, capped) so an ambiguous 429 cannot cause
  a retry storm. Consecutive transient failures open a short per-provider circuit breaker.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from providers.base import ErrorKind

logger = logging.getLogger(__name__)

AVAILABLE, COOLING_DOWN, DISABLED = "available", "cooling_down", "disabled"


@dataclass
class KeyInfo:
    provider: str
    label: str  # e.g. "gemini#1"; safe to log
    secret: str = field(repr=False)
    group: str = ""  # quota group (e.g. the Google Cloud project); keys in a group share daily quota

    @property
    def key_id(self) -> str:
        return f"{self.provider}:{self.label}"


@dataclass
class Cooldown:
    until: float  # epoch seconds
    reason: str  # an ErrorKind value


# ------------------------------------------------------------------------- stores
class HealthStore(ABC):
    @abstractmethod
    def get(self, key_id: str, now: float) -> Cooldown | None: ...

    @abstractmethod
    def put(self, key_id: str, cooldown: Cooldown, now: float) -> None: ...

    @abstractmethod
    def clear(self, key_id: str) -> None: ...


class MemoryHealthStore(HealthStore):
    def __init__(self) -> None:
        self._data: dict[str, Cooldown] = {}
        self._lock = threading.Lock()

    def get(self, key_id: str, now: float) -> Cooldown | None:
        with self._lock:
            cooldown = self._data.get(key_id)
            if cooldown is not None and cooldown.until <= now:
                del self._data[key_id]
                return None
            return cooldown

    def put(self, key_id: str, cooldown: Cooldown, now: float) -> None:
        with self._lock:
            current = self._data.get(key_id)
            if current is None or cooldown.until > current.until:  # never shorten a cooldown
                self._data[key_id] = cooldown

    def clear(self, key_id: str) -> None:
        with self._lock:
            self._data.pop(key_id, None)


class RedisHealthStore(HealthStore):
    """Shares cooldowns between processes. Redis problems degrade to "no shared state"."""

    PREFIX = "rp:llm:cooldown:"

    def __init__(self, url: str, client=None) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(
                url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
            )
        self._r = client

    def get(self, key_id: str, now: float) -> Cooldown | None:
        try:
            raw = self._r.get(self.PREFIX + key_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis health store unavailable (%s); ignoring shared cooldowns", type(exc).__name__
            )
            return None
        if not raw:
            return None
        until_text, _, reason = raw.partition("|")
        try:
            until = float(until_text)
        except ValueError:
            return None
        return Cooldown(until, reason) if until > now else None

    def put(self, key_id: str, cooldown: Cooldown, now: float) -> None:
        ttl_ms = max(1, int((cooldown.until - now) * 1000))
        try:
            existing = self.get(key_id, now)
            if existing is not None and existing.until >= cooldown.until:
                return
            self._r.set(self.PREFIX + key_id, f"{cooldown.until}|{cooldown.reason}", px=ttl_ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis health store write failed (%s)", type(exc).__name__)

    def clear(self, key_id: str) -> None:
        try:
            self._r.delete(self.PREFIX + key_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis health store clear failed (%s)", type(exc).__name__)


# ------------------------------------------------------------------------ helpers
def next_pacific_midnight(now: float) -> float:
    """Epoch seconds of the next midnight in US Pacific time (when Gemini daily quotas reset)."""
    try:
        zone = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:  # no tz database (e.g. Windows without the tzdata package)
        zone = timezone(timedelta(hours=-8))
    local = datetime.fromtimestamp(now, zone)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp() + 60  # a minute of margin


@dataclass
class KeyManagerConfig:
    rate_limit_cooldown_s: float = 30.0  # when the provider gives no retry hint
    max_rate_limit_cooldown_s: float = 3600.0
    quota_default_cooldown_s: float = 3600.0  # daily quota with no known reset time
    credits_cooldown_s: float = 1800.0  # out of credits: recheck periodically
    auth_cooldown_s: float = 21600.0  # rejected key: skip for 6 h, then allow a recheck
    breaker_threshold: int = 3  # consecutive transient failures before the circuit opens
    breaker_cooldown_s: float = 30.0
    jitter: float = 0.1  # up to +10% on cooldowns so workers do not return in lockstep


class KeyManager:
    def __init__(
        self,
        keys: list[KeyInfo],
        store: HealthStore | None = None,
        config: KeyManagerConfig | None = None,
        clock=time.time,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config or KeyManagerConfig()
        self.store = store or MemoryHealthStore()
        self.clock = clock
        self.rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self._lock = threading.RLock()
        self._keys: dict[str, list[KeyInfo]] = {}
        for key in keys:
            self._keys.setdefault(key.provider, []).append(key)
        self._current: dict[str, int] = {p: 0 for p in self._keys}  # sticky preferred key index
        self._consecutive: dict[str, int] = {}  # key_id -> consecutive failures
        self._metrics: dict[str, dict] = {k.key_id: self._blank_metrics() for k in keys}
        self._transient_streak: dict[str, int] = {}
        self._breaker_until: dict[str, float] = {}

    @staticmethod
    def _blank_metrics() -> dict:
        return {"successes": 0, "failures": {}, "failovers": 0, "last_success_at": None, "last_error": None}

    # -------------------------------------------------------------- queries
    def providers(self) -> list[str]:
        return list(self._keys)

    def has_keys(self, provider: str) -> bool:
        return bool(self._keys.get(provider))

    def all_keys(self, provider: str) -> list[KeyInfo]:
        return list(self._keys.get(provider, []))

    def status_of(self, key: KeyInfo) -> tuple[str, Cooldown | None]:
        cooldown = self.store.get(key.key_id, self.clock())
        if cooldown is None:
            return AVAILABLE, None
        return (DISABLED if cooldown.reason == ErrorKind.AUTH_INVALID.value else COOLING_DOWN), cooldown

    def is_available(self, key: KeyInfo) -> bool:
        return self.status_of(key)[0] == AVAILABLE

    def candidates(self, provider: str) -> list[KeyInfo]:
        """Usable keys in a predictable order: the last-good key first, then the rest in
        configured order (wrapping around)."""
        with self._lock:
            keys = self._keys.get(provider, [])
            if not keys:
                return []
            start = self._current.get(provider, 0) % len(keys)
            ordered = keys[start:] + keys[:start]
        return [k for k in ordered if self.is_available(k)]

    def next_available_at(self, provider: str) -> float | None:
        """Earliest time a cooling (not disabled) key of this provider comes back, if any."""
        times = []
        for key in self._keys.get(provider, []):
            state, cooldown = self.status_of(key)
            if state == COOLING_DOWN and cooldown is not None:
                times.append(cooldown.until)
        return min(times) if times else None

    def breaker_open(self, provider: str) -> bool:
        with self._lock:
            return self._breaker_until.get(provider, 0.0) > self.clock()

    # ------------------------------------------------------------- reports
    def report_success(self, key: KeyInfo) -> None:
        with self._lock:
            self._consecutive.pop(key.key_id, None)
            self._transient_streak[key.provider] = 0
            self._breaker_until.pop(key.provider, None)
            metrics = self._metrics[key.key_id]
            metrics["successes"] += 1
            metrics["last_success_at"] = self.clock()
            keys = self._keys[key.provider]
            self._current[key.provider] = keys.index(key)
        self.store.clear(key.key_id)  # a success proves the key works

    def report_failure(
        self, key: KeyInfo, kind: ErrorKind, *, retry_after: float | None = None, scope: str | None = None
    ) -> float:
        """Record a key-scoped failure and put the key (and its quota group, for daily
        quota) into cooldown. Returns the cooldown end time."""
        now = self.clock()
        with self._lock:
            streak = self._consecutive.get(key.key_id, 0) + 1
            self._consecutive[key.key_id] = streak
            metrics = self._metrics[key.key_id]
            metrics["failures"][kind.value] = metrics["failures"].get(kind.value, 0) + 1
            metrics["failovers"] += 1
            metrics["last_error"] = kind.value
        seconds = self._cooldown_seconds(key, kind, retry_after, scope, streak, now)
        until = now + seconds
        targets = [key]
        if kind is ErrorKind.QUOTA_EXHAUSTED and scope in ("day", "credits") and key.group:
            targets = [k for k in self._keys[key.provider] if k.group == key.group]
        for target in targets:
            self.store.put(target.key_id, Cooldown(until, kind.value), now)
        logger.warning(
            "%s %s: %s; skipping for %.0fs%s",
            "Key" if len(targets) == 1 else "Keys",
            ", ".join(t.label for t in targets),
            kind.value,
            seconds,
            " (shared quota group)" if len(targets) > 1 else "",
        )
        return until

    def report_transient(self, provider: str) -> None:
        """A provider failed transiently even after retries; open the breaker after a streak."""
        with self._lock:
            streak = self._transient_streak.get(provider, 0) + 1
            self._transient_streak[provider] = streak
            if streak >= self.config.breaker_threshold:
                self._breaker_until[provider] = self.clock() + self.config.breaker_cooldown_s
                self._transient_streak[provider] = 0
                logger.warning(
                    "Provider %s: %d consecutive transient failures; pausing it for %.0fs",
                    provider,
                    self.config.breaker_threshold,
                    self.config.breaker_cooldown_s,
                )

    def note_failure_metric(self, key: KeyInfo, kind: ErrorKind) -> None:
        """Count a non-key-scoped failure (transient, malformed...) without any cooldown."""
        with self._lock:
            metrics = self._metrics[key.key_id]
            metrics["failures"][kind.value] = metrics["failures"].get(kind.value, 0) + 1
            metrics["last_error"] = kind.value

    def reset(self, label: str | None = None) -> int:
        """Forget cooldowns (all keys, or one label). Returns how many keys were reset."""
        count = 0
        with self._lock:
            for keys in self._keys.values():
                for key in keys:
                    if label is None or key.label == label:
                        self.store.clear(key.key_id)
                        self._consecutive.pop(key.key_id, None)
                        count += 1
            if label is None:
                self._breaker_until.clear()
        return count

    # ------------------------------------------------------------- internals
    def _cooldown_seconds(
        self,
        key: KeyInfo,
        kind: ErrorKind,
        retry_after: float | None,
        scope: str | None,
        streak: int,
        now: float,
    ) -> float:
        cfg = self.config
        if kind is ErrorKind.AUTH_INVALID:
            seconds = cfg.auth_cooldown_s
        elif kind is ErrorKind.QUOTA_EXHAUSTED:
            if scope == "credits":
                seconds = retry_after or cfg.credits_cooldown_s
            elif key.provider == "gemini" and scope == "day":
                seconds = next_pacific_midnight(now) - now  # documented reset: midnight Pacific
            else:
                seconds = retry_after if retry_after else cfg.quota_default_cooldown_s
        else:  # RATE_LIMIT and anything else key-scoped
            base = retry_after if retry_after else cfg.rate_limit_cooldown_s
            seconds = min(cfg.max_rate_limit_cooldown_s, base * (2 ** (streak - 1)))
        seconds = max(1.0, seconds)
        if cfg.jitter and kind is not ErrorKind.QUOTA_EXHAUSTED:
            seconds *= 1 + self.rng.uniform(0, cfg.jitter)
        return seconds

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> list[dict]:
        """Sanitised view of every key: labels and states only, never secrets."""
        now = self.clock()
        rows = []
        for provider, keys in self._keys.items():
            for key in keys:
                state, cooldown = self.status_of(key)
                metrics = self._metrics[key.key_id]
                rows.append(
                    {
                        "provider": provider,
                        "label": key.label,
                        "group": key.group,
                        "status": state,
                        "reason": cooldown.reason if cooldown else None,
                        "available_in_s": round(cooldown.until - now) if cooldown else 0,
                        "successes": metrics["successes"],
                        "failures": dict(metrics["failures"]),
                        "failovers": metrics["failovers"],
                        "last_success_at": metrics["last_success_at"],
                    }
                )
        return rows
