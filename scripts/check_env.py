"""Environment readiness check for ResearchPilot AI.

Usage:
    python scripts/check_env.py          # config, PostgreSQL, Redis (no external API calls)
    python scripts/check_env.py --live   # also makes one lightweight call per configured API key

Secrets are never printed; keys are reported only as "set" / "missing" / "ok" / "rejected".
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

OK, WARN, FAIL = "[ ok ]", "[warn]", "[FAIL]"
failures = 0


def report(status: str, message: str) -> None:
    global failures
    if status == FAIL:
        failures += 1
    print(f"{status} {message}")


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 11):
        report(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        report(FAIL, f"Python {v.major}.{v.minor} found; 3.11+ required")


def check_core_vars() -> None:
    secret = os.getenv("SECRET_KEY", "")
    if len(secret) >= 32:
        report(OK, "SECRET_KEY is set")
    else:
        report(FAIL, "SECRET_KEY missing or shorter than 32 characters")
    if os.getenv("DATABASE_URL"):
        report(OK, "DATABASE_URL is set")
    else:
        report(FAIL, "DATABASE_URL is not set")
    if os.getenv("APP_BASE_URL"):
        report(OK, "APP_BASE_URL is set")
    else:
        report(WARN, "APP_BASE_URL is not set (defaults to http://localhost:5000)")
    mail_keys = ["MAIL_SERVER", "MAIL_PORT", "MAIL_DEFAULT_SENDER"]
    missing = [k for k in mail_keys if not os.getenv(k)]
    if missing:
        report(WARN, f"Email not fully configured; missing: {', '.join(missing)}")
    else:
        report(OK, "SMTP settings present")


def check_database() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        return
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            version = conn.execute(text("select version()")).scalar_one()
        report(OK, f"PostgreSQL reachable ({version.split(',')[0]})")
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        report(FAIL, f"PostgreSQL connection failed: {type(exc).__name__}")


def check_redis() -> None:
    url = os.getenv("REDIS_URL")
    if not url:
        report(WARN, "REDIS_URL not set (needed from Phase 7 for background jobs)")
        return
    try:
        import redis

        redis.Redis.from_url(url, socket_connect_timeout=3).ping()
        report(OK, "Redis reachable")
    except Exception as exc:  # noqa: BLE001
        report(WARN, f"Redis not reachable ({type(exc).__name__}); needed from Phase 7")


def check_provider_keys(live: bool) -> None:
    import requests

    gemini = [f"GEMINI_API_KEY_{i}" for i in range(1, 5)]
    for name in gemini + ["GROQ_API_KEY_1", "OPENROUTER_API_KEY_1", "TAVILY_API_KEY"]:
        value = os.getenv(name, "").strip()
        if not value:
            report(WARN, f"{name} missing")
            continue
        if not live:
            report(OK, f"{name} set (use --live to verify)")
            continue
        try:
            if name.startswith("GEMINI"):
                r = requests.get(
                    "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                    headers={"x-goog-api-key": value},
                    timeout=15,
                )
            elif name.startswith("GROQ"):
                r = requests.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {value}"},
                    timeout=15,
                )
            elif name.startswith("OPENROUTER"):
                r = requests.get(
                    "https://openrouter.ai/api/v1/auth/key",
                    headers={"Authorization": f"Bearer {value}"},
                    timeout=15,
                )
            else:  # Tavily: smallest possible search (uses one credit)
                r = requests.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {value}"},
                    json={"query": "test", "max_results": 1},
                    timeout=20,
                )
        except requests.RequestException as exc:
            report(WARN, f"{name}: network error ({type(exc).__name__})")
            continue
        if r.status_code == 200:
            report(OK, f"{name} accepted by provider")
        elif r.status_code in (400, 401, 403):
            report(FAIL, f"{name} rejected (HTTP {r.status_code})")
        elif r.status_code == 429:
            report(WARN, f"{name} valid but currently rate limited (HTTP 429)")
        else:
            report(WARN, f"{name}: unexpected HTTP {r.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call provider APIs to verify keys")
    args = parser.parse_args()

    if not (ROOT / ".env").exists():
        report(WARN, ".env not found; copy .env.example to .env")
    check_python()
    check_core_vars()
    check_database()
    check_redis()
    check_provider_keys(args.live)
    print()
    print("Result:", "problems found" if failures else "no blocking problems")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
