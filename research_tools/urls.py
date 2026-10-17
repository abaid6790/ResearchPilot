"""URL canonicalisation, de-duplication keys, domains and a heuristic source category."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "ref", "ref_src",
    "ref_url", "source", "spm", "_hsenc", "_hsmi", "mkt_tok", "cmpid", "campaign", "share",
})  # fmt: skip
TRACKING_PREFIXES = ("utm_", "pk_", "hsa_", "ga_")


def _clean_query(query: str) -> str:
    pairs = [
        (k, v) for k, v in parse_qsl(query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]  # fmt: skip
    return urlencode(sorted(pairs))


def canonicalize(url: str) -> str:
    """Normalised form of a URL: lower-case scheme and host, default port and fragment removed,
    tracking parameters dropped, query sorted. Raises ValueError for non-http(s) URLs."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("not an http(s) URL")
    host = parts.hostname.rstrip(".").lower()
    port = parts.port
    default = 443 if parts.scheme.lower() == "https" else 80
    netloc = host if port in (None, default) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, _clean_query(parts.query), ""))


def url_key(url: str) -> str:
    """Key under which two URLs count as the same page (ignores www., trailing slash, http/https)."""
    parts = urlsplit(canonicalize(url))
    host = parts.hostname or ""
    host = host.removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def url_hash(url: str) -> str:
    return hashlib.sha256(url_key(url).encode()).hexdigest()


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    return host.removeprefix("www.")


def domain_matches(domain: str, listed: list[str]) -> bool:
    """True if ``domain`` is one of ``listed`` or a subdomain of one."""
    return any(domain == d or domain.endswith("." + d) for d in listed)


_ACADEMIC = ("arxiv.org", "doi.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "nature.com", "science.org",
             "sciencedirect.com", "springer.com", "link.springer.com", "wiley.com", "jstor.org", "ieee.org",
             "acm.org", "plos.org", "biorxiv.org", "medrxiv.org", "ssrn.com", "researchgate.net",
             "semanticscholar.org")  # fmt: skip
_NEWS = ("reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com", "theguardian.com", "wsj.com",
         "bloomberg.com", "ft.com", "cnn.com", "aljazeera.com", "washingtonpost.com", "dawn.com")  # fmt: skip
_REFERENCE = ("wikipedia.org", "britannica.com", "wikidata.org", "wiktionary.org")
_FORUM = ("reddit.com", "stackoverflow.com", "stackexchange.com", "quora.com", "news.ycombinator.com")
_BLOG = ("medium.com", "substack.com", "wordpress.com", "blogspot.com", "dev.to", "hashnode.dev")
_DOCS_HINT = re.compile(r"^(docs?|developer|developers|api|help|support|learn)\.")


def categorize(url: str) -> str:
    """Best-effort category from the domain alone. A heuristic, not a fact: shown as such."""
    domain = domain_of(url)
    if not domain:
        return "other"
    if (
        domain.endswith((".gov", ".mil"))
        or ".gov." in domain
        or domain.endswith((".europa.eu", ".un.org", ".who.int"))
    ):
        return "government"
    if (
        domain.endswith(".edu")
        or ".edu." in domain
        or ".ac." in domain
        or domain_matches(domain, list(_ACADEMIC))
    ):
        return "academic"
    if domain_matches(domain, list(_REFERENCE)):
        return "reference"
    if domain_matches(domain, list(_NEWS)):
        return "news"
    if domain_matches(domain, list(_FORUM)):
        return "forum"
    if domain_matches(domain, list(_BLOG)):
        return "blog"
    if _DOCS_HINT.match(domain) or domain.endswith(("readthedocs.io", "github.io")):
        return "official_documentation"
    return "other"
