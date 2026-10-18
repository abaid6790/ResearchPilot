"""Citation markers (``[@S<source id>]``) and the deterministic checks on them."""

from __future__ import annotations

import re

CITE = re.compile(r"\[@S(\d+)\]")
_QUOTE = re.compile(r"[\"“]([^\"“”]{20,400})[\"”]")


def cited_ids(body: str) -> list[int]:
    return list(dict.fromkeys(int(m) for m in CITE.findall(body)))


def clean_citations(body: str, allowed: set[int]) -> tuple[str, list[int]]:
    """Remove markers that point at sources the section was not given. Returns (text, removed ids)."""
    removed: list[int] = []

    def fix(match: re.Match) -> str:
        sid = int(match.group(1))
        if sid in allowed:
            return match.group(0)
        removed.append(sid)
        return ""

    cleaned = CITE.sub(fix, body)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, list(dict.fromkeys(removed))


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def unverified_quotes(body: str, source_texts: list[str]) -> list[str]:
    """Quoted passages of five or more words that do not appear in any given source text."""
    haystack = _norm(" ".join(source_texts))
    bad = []
    for match in _QUOTE.finditer(body):
        quote = match.group(1)
        if len(quote.split()) < 5:
            continue
        if _norm(quote) not in haystack:
            bad.append(quote[:120])
    return bad


def uncited_paragraphs(body: str, min_chars: int = 120) -> int:
    """Paragraphs long enough to contain factual statements that carry no citation marker."""
    count = 0
    for para in re.split(r"\n\s*\n", body):
        text = para.strip()
        if len(text) >= min_chars and not text.startswith(("#", "|")) and not CITE.search(text):
            count += 1
    return count
