"""Source scoring, ranking and excerpt preparation.

Scores are heuristics that help order candidates; they are shown as such and never presented
as proof that a source is true. Excerpts are cut verbatim from the retrieved text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from research_tools.urls import categorize, domain_of

CATEGORY_WEIGHT = {
    "government": 0.9, "academic": 0.9, "official_documentation": 0.8, "reference": 0.65, "news": 0.65,
    "industry": 0.6, "company": 0.5, "blog": 0.4, "forum": 0.3, "other": 0.45,
}  # fmt: skip
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "what", "how", "why", "when", "who",
         "does", "about", "into", "their", "have", "has", "can", "will", "than", "between", "using"}  # fmt: skip


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def relevance(query_text: str, title: str, snippet: str, provider_score: float | None = None) -> float:
    """0..1: share of query terms present in title/snippet, blended with the search provider's score."""
    terms = _terms(query_text)
    if not terms:
        return provider_score or 0.0
    haystack = _terms(f"{title} {snippet}")
    overlap = len(terms & haystack) / len(terms)
    title_hit = len(terms & _terms(title)) / len(terms)
    local = min(1.0, 0.7 * overlap + 0.3 * title_hit)
    if provider_score is None:
        return round(local, 3)
    return round(0.5 * local + 0.5 * provider_score, 3)


def quality(url: str, category: str | None = None, published_at: datetime | None = None, text_len: int = 0,
            *, https: bool | None = None) -> float:  # fmt: skip
    """0..1 from category, freshness (small), content volume (small) and https."""
    category = category or categorize(url)
    score = CATEGORY_WEIGHT.get(category, 0.45)
    if published_at is not None:
        age_years = (datetime.now(UTC) - published_at).days / 365
        score += 0.05 if age_years <= 2 else (-0.05 if age_years > 8 else 0.0)
    if text_len:
        score += 0.05 if text_len > 2000 else (-0.1 if text_len < 300 else 0.0)
    if https is None:
        https = url.lower().startswith("https://")
    if not https:
        score -= 0.05
    return round(max(0.0, min(1.0, score)), 3)


def combined(relevance_score: float, quality_score: float) -> float:
    return round(0.6 * relevance_score + 0.4 * quality_score, 3)


def diversify(items: list, key_domain, limit: int, per_domain: int = 2) -> list:
    """Keep the best items first but cap how many come from one domain."""
    out, counts = [], {}
    for item in items:
        d = key_domain(item)
        if counts.get(d, 0) >= per_domain:
            continue
        counts[d] = counts.get(d, 0) + 1
        out.append(item)
        if len(out) >= limit:
            break
    return out


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).lower().encode()).hexdigest()


def chunk_text(text: str, *, target: int = 800, max_chunks: int = 40) -> list[str]:
    """Split into verbatim passages of about ``target`` characters on paragraph/sentence edges."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        pieces = [para] if len(para) <= target * 1.5 else re.split(r"(?<=[.!?])\s+", para)
        for piece in pieces:
            if buf and len(buf) + len(piece) + 1 > target:
                chunks.append(buf)
                buf = piece
            else:
                buf = f"{buf} {piece}".strip() if buf else piece
            if len(buf) > target * 2:
                chunks.append(buf[: target * 2])
                buf = buf[target * 2 :]
    if buf:
        chunks.append(buf)
    chunks = [c for c in chunks if len(c) >= 40]
    return chunks[:max_chunks]


def rank_chunks(chunks: list[str], query_text: str, limit: int) -> list[tuple[str, float]]:
    terms = _terms(query_text)
    scored = []
    for i, ch in enumerate(chunks):
        sc = len(terms & _terms(ch)) / len(terms) if terms else 0.0
        scored.append((i, ch, round(sc, 3)))
    top = sorted(scored, key=lambda t: (-t[2], t[0]))[:limit]
    return [(ch, sc) for _, ch, sc in sorted(top)]


@dataclass
class Candidate:
    url: str
    title: str
    snippet: str
    domain: str
    category: str
    relevance: float
    quality: float
    published_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return combined(self.relevance, self.quality)


def candidates_from_hits(hits, query_text: str) -> list[Candidate]:
    from research_tools.untrusted import injection_signals, sanitize_text

    out = []
    for h in hits:
        snippet = sanitize_text(h.snippet, 4000)
        category = categorize(h.url)
        warnings = [f"possible prompt injection ({s})" for s in injection_signals(f"{h.title}\n{snippet}")]
        out.append(Candidate(
            url=h.url, title=sanitize_text(h.title, 500), snippet=snippet, domain=domain_of(h.url), category=category,
            relevance=relevance(query_text, h.title, snippet, h.score),
            quality=quality(h.url, category, h.published_at, len(snippet)),
            published_at=h.published_at, warnings=warnings,
        ))  # fmt: skip
    return sorted(out, key=lambda c: -c.score)
