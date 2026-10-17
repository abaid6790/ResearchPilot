"""Secure one-time token generation and hashing."""

from __future__ import annotations

import hashlib
import secrets


def generate_token() -> tuple[str, str]:
    """Return ``(raw_token, sha256_hex_digest)``. Only the digest is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
