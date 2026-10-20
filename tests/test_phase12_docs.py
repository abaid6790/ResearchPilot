"""Phase 12: legal pages and documentation checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import BaseConfig
from legal.routes import DOCUMENTS, LEGAL_DIR, render_document

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REQUIRED_TECH_DOCS = [
    "ARCHITECTURE",
    "AGENT_WORKFLOW",
    "API_KEY_ROTATION",
    "API_DOCUMENTATION",
    "DATABASE_SCHEMA",
    "DATABASE_SETUP",
    "ENVIRONMENT_SETUP",
    "INSTALLATION",
    "SECURITY",
    "TESTING",
    "DEPLOYMENT",
    "BACKUP_AND_RESTORE",
    "TROUBLESHOOTING",
    "CONTRIBUTING",
    "CHANGELOG",
    "ROADMAP",
]


@pytest.mark.parametrize("slug", list(DOCUMENTS))
def test_legal_pages_are_public(client, slug):
    resp = client.get(f"/legal/{slug}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    article = html.split("<article")[1].split("</article>")[0]
    assert "<script" not in article and "style=" not in article
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)  # no inline scripts anywhere
    assert 'class="placeholder"' in html  # unfilled placeholders are highlighted


def test_legal_index_and_unknown(client):
    resp = client.get("/legal/")
    assert resp.status_code == 200
    for _file, title, _s in DOCUMENTS.values():
        assert title in resp.get_data(as_text=True)
    assert client.get("/legal/nope").status_code == 404
    assert client.get("/legal/..%2FREADME").status_code == 404


def test_footer_links_on_public_pages(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "/legal/privacy" in html and "/legal/terms" in html


def test_markup_is_sanitised():
    html = str(render_document("# T\n\n<script>alert(1)</script> [x](javascript:alert(1)) [OPERATOR: name]"))
    assert "<script" not in html and "javascript:" not in html
    assert '<mark class="placeholder">[OPERATOR: name]</mark>' in html


@pytest.mark.parametrize("filename", [f for f, _t, _s in DOCUMENTS.values()])
def test_legal_documents_have_content_and_no_invented_claims(filename):
    text = (LEGAL_DIR / filename).read_text(encoding="utf-8")
    assert len(text.split()) > 120
    assert "[OPERATOR:" in text
    assert re.search(r"legal review|Contact and Support", text) or filename == "CONTACT_AND_SUPPORT.md"
    for banned in ("we are GDPR compliant", "CCPA compliant", "ISO 27001 certified", "SOC 2"):
        assert banned.lower() not in text.lower()


def test_privacy_policy_discloses_external_providers():
    text = (LEGAL_DIR / "PRIVACY_POLICY.md").read_text(encoding="utf-8").lower()
    for name in ("gemini", "groq", "openrouter", "tavily"):
        assert name in text
    assert "never reaches" not in text


def test_cookie_policy_matches_configuration():
    text = (LEGAL_DIR / "COOKIE_POLICY.md").read_text(encoding="utf-8")
    assert f"`{BaseConfig.SESSION_COOKIE_NAME}`" in text
    assert "rp-theme" in text
    js = (ROOT / "static" / "js" / "theme-init.js").read_text(encoding="utf-8")
    assert "rp-theme" in js
    assert BaseConfig.SESSION_COOKIE_HTTPONLY is True and BaseConfig.SESSION_COOKIE_SAMESITE == "Lax"


@pytest.mark.parametrize("name", REQUIRED_TECH_DOCS)
def test_technical_docs_exist_with_body(name):
    text = (DOCS / f"{name}.md").read_text(encoding="utf-8")
    body = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert len(body) >= 8, f"{name}.md looks like headings only"


def test_docs_do_not_contain_real_looking_keys():
    pattern = re.compile(
        r"(AIza[0-9A-Za-z_-]{30,}|gsk_[0-9A-Za-z]{30,}|tvly-[0-9A-Za-z]{20,}|sk-or-v1-[0-9a-f]{20,})"
    )
    for path in list(DOCS.rglob("*.md")) + [ROOT / "README.md", ROOT / ".env.example"]:
        assert not pattern.search(path.read_text(encoding="utf-8")), path
