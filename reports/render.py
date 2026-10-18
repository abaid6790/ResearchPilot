"""Rendering of a report: plain Markdown, and safe HTML with numbered citations."""

from __future__ import annotations

import re

import bleach
import markdown

from agents.citations import CITE
from reports.models import ReportDocument


def to_markdown(report: ReportDocument) -> str:
    parts = [f"# {report.title}"]
    for section in report.sections:
        parts.append(f"{'#' * (section.level + 1)} {section.heading}\n\n{section.body}")
    return "\n\n".join(parts) + "\n"


# --------------------------------------------------------------------------- HTML
_TAGS = ["p", "br", "ul", "ol", "li", "strong", "em", "code", "pre", "blockquote", "h3", "h4", "h5", "table",
         "thead", "tbody", "tr", "th", "td", "sup"]  # fmt: skip


def number_sources(report: ReportDocument) -> dict[int, int]:
    """Source id -> citation number, in order of first appearance in the body sections."""
    order: dict[int, int] = {}
    for section in report.sections:
        if section.key == "references":
            continue
        for match in CITE.finditer(section.body):
            order.setdefault(int(match.group(1)), len(order) + 1)
    return order


def section_html(body: str, numbers: dict[int, int]) -> str:
    """Markdown from a model or a user -> safe HTML. Sanitised first, citations added afterwards, so
    nothing in the text can inject markup or links; markers to unknown sources are dropped."""
    html = markdown.markdown(body, extensions=["sane_lists", "tables"], output_format="html")
    html = bleach.clean(html, tags=_TAGS, attributes={}, strip=True)

    def cite(match: re.Match) -> str:
        n = numbers.get(int(match.group(1)))
        if n is None:
            return ""
        return f'<sup class="cite"><a href="#ref-{n}" aria-label="Source {n}">[{n}]</a></sup>'

    return CITE.sub(cite, html)


def to_plain_text(report: ReportDocument, sources: dict) -> str:
    """Copyable text: citation markers become [n] and a numbered reference list follows the body.
    ``sources`` maps source id -> ResearchSource. Markers to unknown sources are dropped."""
    from sqlalchemy import select

    from extensions import db
    from research.models import ResearchSource

    numbers = number_sources(report)
    if not sources and numbers:
        sources = {
            s.id: s for s in db.session.scalars(select(ResearchSource).where(ResearchSource.id.in_(numbers)))
        }

    def mark(match: re.Match) -> str:
        n = numbers.get(int(match.group(1)))
        return f"[{n}]" if n and int(match.group(1)) in sources else ""

    parts = [f"# {report.title}"]
    for section in report.sections:
        if section.key == "references":
            continue
        parts.append(f"{'#' * (section.level + 1)} {section.heading}\n\n{CITE.sub(mark, section.body)}")
    refs = []
    for sid, n in sorted(numbers.items(), key=lambda kv: kv[1]):
        src = sources.get(sid)
        if src:
            refs.append(
                f"[{n}] {src.title or src.domain or src.upload_name}" + (f" - {src.url}" if src.url else "")
            )
    if refs:
        parts.append("## References\n\n" + "\n".join(refs))
    return "\n\n".join(parts) + "\n"
