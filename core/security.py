"""Security headers, CSP and redirect safety helpers."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from flask import Flask, Response, request

# The frontend uses only same-origin scripts, styles and images (no inline
# code, no third-party CDNs), so the policy can be strict.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def register_security_headers(app: Flask) -> None:
    @app.after_request
    def add_headers(response: Response) -> Response:
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if app.config.get("ENV_NAME") == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Authenticated and token pages must not be cached by browsers or proxies.
        if request.endpoint and not request.endpoint.startswith("static"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def is_safe_redirect_target(target: str | None) -> bool:
    """Allow only same-site, relative redirect targets (prevents open redirects)."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return False
    if "\\" in target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in {"http", "https"} and redirect_url.netloc == host_url.netloc
