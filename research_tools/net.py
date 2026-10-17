"""Network safety primitives (SSRF defences).

A URL is only fetched if every address its host resolves to is a public, globally routable
address. The check happens *at connection time* inside the socket layer and the connection is
made to the address that was checked, so DNS rebinding (a name that resolves to a public
address for the check and a private one for the connection) cannot bypass it.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import LocationParseError, NewConnectionError

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
MAX_URL_LENGTH = 2048


class UnsafeURLError(ValueError):
    """The URL (or an address it resolves to) is not allowed to be fetched."""


# Ranges that ``ipaddress`` does not already treat as non-global, or that we want to be explicit about.
_EXTRA_BLOCKED = [ipaddress.ip_network(n) for n in (
    "100.64.0.0/10",   # carrier-grade NAT
    "192.0.0.0/24",    # IETF protocol assignments
    "198.18.0.0/15",   # benchmarking
    "2002::/16",       # 6to4 (embeds an IPv4 address)
    "2001::/32",       # Teredo
    "64:ff9b::/96",    # NAT64
)]  # fmt: skip


def is_public_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(value) if isinstance(value, str) else value
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:  # ::ffff:127.0.0.1
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified or not ip.is_global
    ):  # fmt: skip
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED if net.version == ip.version)


Resolver = Callable[[str, int], list[str]]
IpPolicy = Callable[[str], bool]


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def check_url(url: str, *, ports: frozenset[int] = ALLOWED_PORTS) -> tuple[str, str, int]:
    """Cheap static checks. Returns ``(scheme, hostname, port)`` or raises :class:`UnsafeURLError`."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError("URL is empty or too long")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        raise UnsafeURLError("URL contains whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("URL is not valid") from exc
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError("only http and https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in ports:
        raise UnsafeURLError(f"port {port} is not allowed")
    return parts.scheme, host, port


def make_guarded_session(
    resolver: Resolver = system_resolver,
    ip_ok: IpPolicy = is_public_ip,
    ports: frozenset[int] = ALLOWED_PORTS,
) -> requests.Session:
    """A ``requests`` session whose every new connection is validated and pinned to a checked IP.

    Environment proxies are ignored (``trust_env=False``): a proxy would do the DNS lookup and
    connection itself, outside this check.
    """

    def pick_address(host: str, port: int) -> str:
        if port not in ports:
            raise UnsafeURLError(f"port {port} is not allowed")
        try:
            addresses = resolver(host, port)
        except OSError as exc:
            raise UnsafeURLError("host name could not be resolved") from exc
        if not addresses:
            raise UnsafeURLError("host name could not be resolved")
        blocked = [a for a in addresses if not ip_ok(a)]
        if blocked:  # refuse if ANY answer is internal: a mixed answer is a rebinding signal
            raise UnsafeURLError("host resolves to a non-public address")
        return addresses[0]

    class _Mixin:
        def _new_conn(self):
            try:
                self._dns_host = pick_address(self._dns_host, self.port)  # connect to the checked IP
            except UnsafeURLError as exc:
                raise NewConnectionError(self, f"blocked: {exc}") from exc
            return super()._new_conn()

    class GuardedHTTPConnection(_Mixin, HTTPConnection):
        pass

    class GuardedHTTPSConnection(_Mixin, HTTPSConnection):
        pass

    class GuardedHTTPPool(HTTPConnectionPool):
        ConnectionCls = GuardedHTTPConnection

    class GuardedHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = GuardedHTTPSConnection

    class GuardedAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"http": GuardedHTTPPool, "https": GuardedHTTPSPool}

        def proxy_manager_for(self, *args, **kwargs):  # never tunnel through a proxy
            raise UnsafeURLError("proxies are not used for page fetching")

    session = requests.Session()
    session.trust_env = False
    adapter = GuardedAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


__all__ = ["LocationParseError", "UnsafeURLError", "check_url", "is_public_ip", "make_guarded_session"]
