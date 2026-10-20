"""Public legal pages: the Markdown documents in docs/legal rendered as safe HTML.

The files are the single source of truth. Text written as ``[OPERATOR: ...]`` is a placeholder the
operator must replace; the page highlights each one so none is missed.
"""

from __future__ import annotations

import re
from pathlib import Path

import bleach
import markdown
from flask import Blueprint, abort, render_template
from markupsafe import Markup

bp = Blueprint("legal", __name__, url_prefix="/legal")

LEGAL_DIR = Path(__file__).resolve().parent.parent / "docs" / "legal"

# slug -> (file, title, one-line summary)
DOCUMENTS: dict[str, tuple[str, str, str]] = {
    "privacy": ("PRIVACY_POLICY.md", "Privacy Policy", "What is stored, who receives it, your choices."),
    "cookies": ("COOKIE_POLICY.md", "Cookie Policy", "The cookie and browser storage the app uses."),
    "terms": ("TERMS_OF_SERVICE.md", "Terms of Service", "The rules for using the service."),
    "acceptable-use": ("ACCEPTABLE_USE_POLICY.md", "Acceptable Use Policy", "What you must not do."),
    "ai-usage": (
        "AI_USAGE_AND_LIMITATIONS.md",
        "AI Usage and Limitations",
        "What AI-drafted reports can and cannot be trusted for.",
    ),
    "retention": (
        "DATA_RETENTION_POLICY.md",
        "Data Retention Policy",
        "How long data is kept and how deletion works.",
    ),
    "security": ("SECURITY_DISCLOSURE.md", "Security Disclosure", "How to report a vulnerability."),
    "contact": ("CONTACT_AND_SUPPORT.md", "Contact and Support", "Who to contact and what to try first."),
}

_TAGS = [
    "p",
    "br",
    "strong",
    "em",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
    "hr",
]
_PLACEHOLDER = re.compile(r"\[OPERATOR:[^\]]*\]")


def render_document(text: str) -> Markup:
    html = markdown.markdown(text, extensions=["sane_lists", "tables"], output_format="html")
    html = bleach.clean(
        html, tags=_TAGS, attributes={"a": ["href"]}, protocols=["http", "https", "mailto"], strip=True
    )
    html = _PLACEHOLDER.sub(lambda m: f'<mark class="placeholder">{m.group(0)}</mark>', html)
    return Markup(html)  # noqa: S704


def placeholder_count(text: str) -> int:
    return len(_PLACEHOLDER.findall(text))


def load(slug: str) -> tuple[str, str]:
    entry = DOCUMENTS.get(slug)
    if entry is None:
        abort(404)
    path = LEGAL_DIR / entry[0]
    try:
        return entry[1], path.read_text(encoding="utf-8")
    except OSError:
        abort(404)


@bp.get("/")
def index():
    items = []
    for slug, (_file, title, summary) in DOCUMENTS.items():
        _t, text = load(slug)
        items.append({"slug": slug, "title": title, "summary": summary, "open": placeholder_count(text)})
    return render_template("legal/index.html", items=items)


@bp.get("/<slug>")
def document(slug: str):
    title, text = load(slug)
    return render_template(
        "legal/document.html",
        title=title,
        slug=slug,
        body=render_document(text),
        open_placeholders=placeholder_count(text),
        documents=DOCUMENTS,
    )
