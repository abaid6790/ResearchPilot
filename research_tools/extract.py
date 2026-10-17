"""Readable text and metadata from HTML (lxml; no scripts are executed)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import lxml.html
from charset_normalizer import from_bytes
from lxml import etree

from research_tools.untrusted import sanitize_text

_DROP_TAGS = ("script", "style", "noscript", "nav", "footer", "aside", "form", "header", "iframe", "svg",
              "template", "button", "select", "dialog")  # fmt: skip
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "li", "ul", "ol", "br", "tr", "table", "blockquote",
               "h1", "h2", "h3", "h4", "h5", "h6", "pre"}  # fmt: skip
MAX_HTML_CHARS = 3_000_000


@dataclass
class Extracted:
    title: str | None = None
    text: str = ""
    canonical_url: str | None = None
    published_at: datetime | None = None
    language: str | None = None
    warnings: list[str] = field(default_factory=list)


def decode_bytes(raw: bytes, declared: str | None = None) -> str:
    if declared:
        try:
            return raw.decode(declared, errors="replace")
        except LookupError:
            pass
    best = from_bytes(raw[:200_000]).best()
    if best is not None:
        return raw.decode(best.encoding, errors="replace")
    return raw.decode("utf-8", errors="replace")


_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d")


def parse_date(value: str | None) -> datetime | None:
    """Parse a date only if it is clearly one; never guess. Dates in the future are rejected."""
    if not value:
        return None
    value = value.strip().replace("Z", "+0000")
    value = re.sub(r"([+-]\d\d):(\d\d)$", r"\1\2", value)
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if parsed > datetime.now(UTC) or parsed.year < 1990:
            return None
        return parsed.astimezone(UTC)
    return None


def _meta(tree, *names: str) -> str | None:
    for name in names:
        for attr in ("property", "name", "itemprop"):
            found = tree.xpath(f'//meta[@{attr}="{name}"]/@content')
            if found and found[0].strip():
                return found[0].strip()
    return None


def _json_ld_dates(tree) -> str | None:
    for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            data = json.loads(script)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("datePublished"):
                    return str(item["datePublished"])
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
            elif isinstance(item, list):
                stack.extend(item)
    return None


def extract_html(raw: bytes, declared_encoding: str | None = None, *, max_chars: int = 200_000) -> Extracted:
    text = decode_bytes(raw, declared_encoding)[:MAX_HTML_CHARS]
    result = Extracted()
    if not text.strip():
        result.warnings.append("empty page")
        return result
    try:
        tree = lxml.html.fromstring(text)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        result.warnings.append("page could not be parsed")
        return result

    title = _meta(tree, "og:title", "twitter:title") or (tree.findtext(".//title") or "").strip()
    result.title = sanitize_text(" ".join(title.split()), 500) or None
    canon = tree.xpath('//link[@rel="canonical"]/@href')
    result.canonical_url = canon[0].strip() if canon else None
    html_lang = tree.get("lang")
    result.language = html_lang.split("-")[0].lower() if html_lang else None

    date_raw = (
        _meta(tree, "article:published_time", "og:published_time", "datePublished", "date", "DC.date.issued",
              "citation_publication_date", "citation_date", "pubdate")
        or _json_ld_dates(tree)
        or next(iter(tree.xpath("//time[@datetime]/@datetime")), None)
    )  # fmt: skip
    result.published_at = parse_date(date_raw)

    for tag in _DROP_TAGS:
        for element in tree.xpath(f"//{tag}"):
            element.drop_tree()
    for comment in tree.xpath("//comment()"):
        comment.drop_tree()

    root = None
    for xpath in ("//article", "//main", '//*[@role="main"]'):
        found = tree.xpath(xpath)
        if found:
            root = max(found, key=lambda e: len(e.text_content()))
            if len(root.text_content().strip()) > 300:
                break
            root = None
    if root is None:
        body = tree.find(".//body")
        root = body if body is not None else tree

    for element in root.iter():
        if element.tag in _BLOCK_TAGS:
            element.tail = "\n" + (element.tail or "")
            if element.tag in ("br",):
                continue
            element.text = element.text or ""
    content = root.text_content()
    result.text = sanitize_text(content, max_chars)
    if len(result.text) < 200:
        result.warnings.append("very little readable text (page may need JavaScript or be paywalled)")
    return result
