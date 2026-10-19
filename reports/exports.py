"""Report export: Markdown, HTML, DOCX and PDF.

Everything in a report is untrusted text (it came from web pages through a model, or from the
user). Each format therefore escapes it for that format: HTML goes through the same sanitiser as
the report page, DOCX and PDF text is inserted as plain runs / escaped paragraph markup, and
only ``http(s)`` addresses ever become links. Citations ``[@S12]`` become ``[n]`` numbers that
link to the numbered reference list where the format supports links.

An export is allowed for any report its owner can open. When the report has not passed its
quality gates, the cover and the document say so, and a quality section lists the warnings.
"""

from __future__ import annotations

import html as html_lib
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy import select

from agents.citations import CITE
from core.db_utils import utcnow
from extensions import db
from reports import quality
from reports.models import ReportDocument
from reports.render import number_sources, section_html
from research.models import ResearchJob, ResearchProject, ResearchSource

FORMATS = {
    "md": ("text/markdown; charset=utf-8", "md"),
    "html": ("text/html; charset=utf-8", "html"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "pdf": ("application/pdf", "pdf"),
}
TOC_MIN_SECTIONS = 4
_CITE_OPEN, _CITE_CLOSE = "\x01", "\x02"  # sentinels: only real citation markers become links
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_url(url: str | None) -> str | None:
    return (
        url if url and url.lower().startswith(("http://", "https://")) and not _CONTROL.search(url) else None
    )


def clean(text: str | None) -> str:
    return _CONTROL.sub("", text or "")


# ------------------------------------------------------------------------------ model
@dataclass
class Ref:
    n: int
    text: str
    url: str | None
    note: str = ""


@dataclass
class ExportDoc:
    title: str
    generated: datetime
    scope: list[tuple[str, str]]
    methodology: list[str]
    sections: list[tuple[str, str, str]]  # (key, heading, body with [@S<id>] markers)
    numbers: dict[int, int]
    refs: list[Ref]
    verified: bool
    notice: str | None
    overall: float | None
    gates: list[quality.Gate]
    warnings: list[dict]
    scores: list[quality.Score]
    opinions: list = field(default_factory=list)  # llm QualityEvaluation rows, labelled as opinions
    language: str = "en"

    @property
    def toc(self) -> bool:
        return len(self.sections) >= TOC_MIN_SECTIONS


def _scope(project: ResearchProject, job: ResearchJob | None) -> list[tuple[str, str]]:
    st = (job.config_snapshot if job else None) or project.settings or {}
    rows = [("Topic", project.topic)]
    if project.objectives:
        rows.append(("Objectives", project.objectives))
    rows += [("Depth", str(st.get("depth", "standard"))), ("Audience", str(st.get("audience", "general"))),
             ("Language", str(st.get("language", "en"))), ("Writing style", str(st.get("writing_style", "neutral")))]  # fmt: skip
    if st.get("date_from") or st.get("date_to"):
        rows.append(("Date range", f"{st.get('date_from') or 'any'} to {st.get('date_to') or 'any'}"))
    if st.get("prioritize_domains"):
        rows.append(("Preferred domains", ", ".join(st["prioritize_domains"])))
    if st.get("exclude_domains"):
        rows.append(("Excluded domains", ", ".join(st["exclude_domains"])))
    return rows


def _methodology(job: ResearchJob | None) -> list[str]:
    if job is None:
        return ["The run that produced this report is no longer stored, so its methodology cannot be listed."]
    from research.services import job_summary

    s = job_summary(job)
    critic = (job.config_snapshot or {}).get("enable_critic", True)
    out = [
        f"The question was split into {len(s['tasks'])} subtopic(s) and searched with {s['queries']} web search(es) "
        f"over {s['research_round']} research round(s).",
        f"{s['sources']} source(s) were collected; {s['sources_read']} were read in full and the rest were used "
        "through search snippets or could not be read.",
        f"{s['claims']} claim(s) were extracted from stored excerpts and the report was written from those claims only.",
        f"The draft went through {s['revision_round']} revision round(s)"
        + (
            " after an independent AI review."
            if critic
            else "; the independent review was switched off for this run."
        ),
        "Whether each excerpt supports its claim was judged by an AI model. A reachable web page is not treated as proof.",
    ]
    return [x for x in out if x]


def build_model(report: ReportDocument) -> ExportDoc:
    project = db.session.get(ResearchProject, report.project_id)
    job = db.session.get(ResearchJob, report.job_id) if report.job_id else None
    evaluation = quality.compute(report)
    numbers = number_sources(report)
    srcs = {
        s.id: s
        for s in db.session.scalars(select(ResearchSource).where(ResearchSource.id.in_(numbers or {0})))
    }
    numbers = {sid: n for sid, n in numbers.items() if sid in srcs}
    refs = []
    for sid, n in sorted(numbers.items(), key=lambda kv: kv[1]):
        s = srcs[sid]
        bits = []
        if s.published_at:
            bits.append(f"published {s.published_at.strftime('%Y-%m-%d')}")
        if s.retrieved_at:
            bits.append(f"retrieved {s.retrieved_at.strftime('%Y-%m-%d')}")
        bits.append(
            "read in full"
            if s.fetch_status == "retrieved"
            else "search snippet only"
            if s.fetch_status == "snippet_only"
            else s.fetch_status
        )
        label = clean(s.title or s.domain or s.upload_name or "Untitled")
        where = (
            clean(s.url)
            if safe_url(s.url)
            else (f"uploaded document: {clean(s.upload_name)}" if s.upload_name else "")
        )
        refs.append(
            Ref(
                n,
                label,
                safe_url(s.url),
                "; ".join(bits) + (f" ({where})" if where and not safe_url(s.url) else ""),
            )
        )
    verified = bool(report.export_ready) and evaluation.gates_passed
    notice = (
        None
        if verified
        else (
            "NOT FULLY VERIFIED. The automatic quality checks found problems, or the text was edited after they ran. "
            "See the quality section before relying on this report."
        )
    )
    return ExportDoc(
        title=clean(report.title),
        generated=utcnow(),
        scope=_scope(project, job),
        methodology=_methodology(job),
        sections=[(s.key, clean(s.heading), clean(s.body)) for s in report.sections if s.key != "references"],
        numbers=numbers,
        refs=refs,
        verified=verified,
        notice=notice,
        overall=evaluation.overall,
        gates=evaluation.gates,
        warnings=evaluation.warnings,
        scores=evaluation.scores,
        opinions=quality.latest_rows(report, "llm"),
        language=(job.config_snapshot or {}).get("language", "en") if job else "en",
    )


def marked(body: str, doc: ExportDoc, *, plain: bool = False) -> str:
    """Replace citation markers by [n] (or sentinels around n). Unknown sources are dropped."""

    def sub(m: re.Match) -> str:
        n = doc.numbers.get(int(m.group(1)))
        if n is None:
            return ""
        return f"[{n}]" if plain else f"{_CITE_OPEN}{n}{_CITE_CLOSE}"

    return CITE.sub(sub, body)


def _score_text(score: float | None) -> str:
    return "not assessed" if score is None else f"{score:.0f}/100"


def quality_lines(doc: ExportDoc) -> list[str]:
    lines = [
        f"Overall (deterministic): {_score_text(doc.overall)}. This is a calculated score, not proof that the statements are true."
    ]
    for s in doc.scores:
        if s.dimension != "overall":
            lines.append(f"{quality.LABELS[s.dimension]}: {_score_text(s.score)}")
    return lines


# ------------------------------------------------------------------------------ markdown
def build_markdown(doc: ExportDoc) -> bytes:
    esc = lambda t: t.replace("<", "&lt;")  # noqa: E731 - keeps raw HTML in text inert when rendered
    out = [
        f"# {esc(doc.title)}",
        f"*Generated {doc.generated.strftime('%d %B %Y, %H:%M')} UTC by ResearchPilot AI*",
    ]
    if doc.notice:
        out.append(f"> **{doc.notice}**")
    out.append("## Research scope\n\n" + "\n".join(f"- **{k}:** {esc(v)}" for k, v in doc.scope))
    out.append("## Methodology\n\n" + "\n".join(f"- {esc(m)}" for m in doc.methodology))
    if doc.toc:
        out.append(
            "## Contents\n\n" + "\n".join(f"{i}. {esc(h)}" for i, (_, h, _) in enumerate(doc.sections, 1))
        )
    for _, heading, body in doc.sections:
        out.append(f"## {esc(heading)}\n\n{esc(marked(body, doc, plain=True))}")
    out.append("## Quality assessment\n\n" + "\n".join(f"- {x}" for x in quality_lines(doc)))
    if doc.warnings:
        out.append("### Warnings\n\n" + "\n".join(f"- {esc(w['text'])}" for w in doc.warnings))
    for o in doc.opinions:
        out.append(
            f"- AI opinion (not a fact check): {quality.LABELS.get(o.dimension, o.dimension)} {_score_text(o.score)}"
        )
    refs = [
        f"{r.n}. {esc(r.text)}" + (f" - <{r.url}>" if r.url else "") + (f" ({r.note})" if r.note else "")
        for r in doc.refs
    ]
    out.append("## References\n\n" + ("\n".join(refs) if refs else "No sources were cited."))
    return ("\n\n".join(out) + "\n").encode("utf-8")


# ------------------------------------------------------------------------------ html
_CSS = (
    "body{font:16px/1.65 Georgia,'Times New Roman',serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a}"
    "h1{font-size:2rem;line-height:1.2}h2{margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.2rem}"
    ".notice{border:2px solid #b45309;background:#fffbeb;padding:.75rem 1rem;font-weight:bold}"
    ".meta{color:#555}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3rem .6rem}"
    "sup.cite a{text-decoration:none}.refs li{margin:.4rem 0}.note{color:#555;font-size:.9rem}a{color:#0b4f8a}"
    "@media print{a{color:inherit;text-decoration:none}}"
)


def build_html(doc: ExportDoc) -> bytes:
    e = html_lib.escape
    parts = [
        f"<h1>{e(doc.title)}</h1>",
        f'<p class="meta">Generated {e(doc.generated.strftime("%d %B %Y, %H:%M"))} UTC by ResearchPilot AI</p>',
    ]
    if doc.notice:
        parts.append(f'<p class="notice" role="alert">{e(doc.notice)}</p>')
    parts.append(
        "<h2>Research scope</h2><dl>"
        + "".join(f"<dt><strong>{e(k)}</strong></dt><dd>{e(v)}</dd>" for k, v in doc.scope)
        + "</dl>"
    )
    parts.append("<h2>Methodology</h2><ul>" + "".join(f"<li>{e(m)}</li>" for m in doc.methodology) + "</ul>")
    if doc.toc:
        parts.append(
            '<nav aria-label="Contents"><h2>Contents</h2><ol>'
            + "".join(
                f'<li><a href="#sec-{i}">{e(h)}</a></li>' for i, (_, h, _) in enumerate(doc.sections, 1)
            )
            + "</ol></nav>"
        )
    for i, (_, heading, body) in enumerate(doc.sections, 1):
        parts.append(
            f'<section id="sec-{i}"><h2>{e(heading)}</h2>{section_html(body, doc.numbers)}</section>'
        )
    parts.append(
        '<section id="quality"><h2>Quality assessment</h2><ul>'
        + "".join(f"<li>{e(x)}</li>" for x in quality_lines(doc))
        + "</ul>"
    )
    if doc.warnings:
        parts.append(
            "<h3>Warnings</h3><ul>" + "".join(f"<li>{e(w['text'])}</li>" for w in doc.warnings) + "</ul>"
        )
    if doc.opinions:
        parts.append(
            '<p class="note">AI opinions (rubric-based, not a fact check): '
            + e(
                "; ".join(
                    f"{quality.LABELS.get(o.dimension, o.dimension)} {_score_text(o.score)}"
                    for o in doc.opinions
                )
            )
            + "</p>"
        )
    parts.append("</section>")
    refs = []
    for r in doc.refs:
        link = (
            f' <a href="{e(r.url, quote=True)}" rel="noopener noreferrer nofollow">{e(r.url)}</a>'
            if r.url
            else ""
        )
        refs.append(
            f'<li id="ref-{r.n}">[{r.n}] {e(r.text)}{link} <span class="note">{e(r.note)}</span></li>'
        )
    parts.append(
        '<section id="references"><h2>References</h2>'
        + (
            '<ol class="refs" style="list-style:none;padding:0">' + "".join(refs) + "</ol>"
            if refs
            else "<p>No sources were cited.</p>"
        )
        + "</section>"
    )
    page = (
        f'<!doctype html><html lang="{e(doc.language)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(doc.title)}</title><style>{_CSS}</style></head><body><article>{''.join(parts)}</article></body></html>"
    )
    return page.encode("utf-8")


# ------------------------------------------------------------------------------ block parsing (docx, pdf)
_INLINE = re.compile(
    rf"{_CITE_OPEN}(\d+){_CITE_CLOSE}|\*\*(.+?)\*\*|`([^`]+)`|(?<![\w*])\*([^*\n]+)\*(?![\w*])"
)


def parse_inline(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos : m.start()]))
        if m.group(1):
            out.append(("cite", m.group(1)))
        elif m.group(2):
            out.append(("bold", m.group(2)))
        elif m.group(3):
            out.append(("code", m.group(3)))
        else:
            out.append(("italic", m.group(4)))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return [
        (k, v.replace(_CITE_OPEN, "").replace(_CITE_CLOSE, "")) if k != "cite" else (k, v) for k, v in out
    ]


def parse_blocks(body: str) -> list[tuple]:
    blocks: list[tuple] = []
    para: list[str] = []
    items: list[tuple[str, list[str]]] | None = None  # (kind, lines)

    def flush():
        nonlocal para, items
        if para:
            blocks.append(("p", " ".join(para)))
            para = []
        if items:
            blocks.append((items[0][0], [" ".join(x[1]) for x in items]))
            items = None

    table: list[list[str]] = []
    for raw in body.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|"):
            flush()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                table.append(cells)
            continue
        if table:
            blocks.append(("table", table))
            table = []
        if not stripped:
            flush()
            continue
        m_bullet = re.match(r"^[-*+]\s+(.*)", stripped)
        m_num = re.match(r"^\d+[.)]\s+(.*)", stripped)
        m_head = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m_head:
            flush()
            blocks.append(("h", len(m_head.group(1)), m_head.group(2)))
        elif m_bullet or m_num:
            kind = "ul" if m_bullet else "ol"
            if para:
                blocks.append(("p", " ".join(para)))
                para = []
            if items and items[0][0] != kind:
                flush()
            items = items or []
            items.append((kind, [(m_bullet or m_num).group(1)]))
        elif stripped.startswith(">"):
            flush()
            blocks.append(("quote", stripped.lstrip("> ").strip()))
        elif items and raw.startswith((" ", "\t")):
            items[-1][1].append(stripped)
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    if table:
        blocks.append(("table", table))
    return blocks


# ------------------------------------------------------------------------------ docx
def _docx_bookmark(paragraph, name: str, ident: int) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(ident))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(ident))
    paragraph._p.insert(0, start)  # noqa: SLF001
    paragraph._p.append(end)  # noqa: SLF001


def _docx_link(paragraph, text: str, *, anchor: str | None = None, url: str | None = None) -> None:
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    link = OxmlElement("w:hyperlink")
    if anchor:
        link.set(qn("w:anchor"), anchor)
    else:
        link.set(qn("r:id"), paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True))
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0B4F8A")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(color)
    props.append(underline)
    run.append(props)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    paragraph._p.append(link)  # noqa: SLF001


def _docx_inline(paragraph, text: str) -> None:
    for kind, value in parse_inline(text):
        if kind == "cite":
            _docx_link(paragraph, f"[{value}]", anchor=f"ref{value}")
        else:
            run = paragraph.add_run(value)
            run.bold = kind == "bold"
            run.italic = kind == "italic"
            if kind == "code":
                run.font.name = "Consolas"


def build_docx(doc: ExportDoc) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    d = Document()
    d.core_properties.title = doc.title[:200]
    d.core_properties.author = "ResearchPilot AI"
    for sec in d.sections:
        sec.left_margin = sec.right_margin = Cm(2.5)
        sec.top_margin = sec.bottom_margin = Cm(2.3)
    normal = d.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15

    # footer with page number
    footer = d.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run("Page ")
    run = footer.add_run()
    for tag, text in (("begin", None), (None, " PAGE "), ("end", None)):
        if tag:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), tag)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)  # noqa: SLF001

    # cover
    for _ in range(5):
        d.add_paragraph()
    title = d.add_paragraph(doc.title, style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    d.add_paragraph(f"Generated {doc.generated.strftime('%d %B %Y, %H:%M')} UTC by ResearchPilot AI").runs[
        0
    ].italic = True
    if doc.notice:
        p = d.add_paragraph()
        r = p.add_run(doc.notice)
        r.bold = True
        r.font.color.rgb = RGBColor(0xB4, 0x53, 0x09)
    d.add_page_break()

    d.add_heading("Research scope", level=1)
    for k, v in doc.scope:
        p = d.add_paragraph(style="List Bullet")
        p.add_run(f"{k}: ").bold = True
        p.add_run(v)
    d.add_heading("Methodology", level=1)
    for m in doc.methodology:
        d.add_paragraph(m, style="List Bullet")

    ident = 1
    if doc.toc:
        d.add_heading("Contents", level=1)
        for i, (_, heading, _) in enumerate(doc.sections, 1):
            p = d.add_paragraph()
            _docx_link(p, f"{i}. {heading}", anchor=f"sec{i}")
    d.add_page_break() if doc.toc else None

    for i, (_, heading, body) in enumerate(doc.sections, 1):
        h = d.add_heading(heading, level=1)
        _docx_bookmark(h, f"sec{i}", ident)
        ident += 1
        _docx_blocks(d, parse_blocks(marked(body, doc)))

    d.add_heading("Quality assessment", level=1)
    for line in quality_lines(doc):
        d.add_paragraph(line, style="List Bullet")
    for o in doc.opinions:
        d.add_paragraph(
            f"AI opinion (not a fact check): {quality.LABELS.get(o.dimension, o.dimension)} {_score_text(o.score)}",
            style="List Bullet",
        )
    if doc.warnings:
        d.add_heading("Warnings", level=2)
        for w in doc.warnings:
            d.add_paragraph(w["text"], style="List Bullet")

    d.add_heading("References", level=1)
    if not doc.refs:
        d.add_paragraph("No sources were cited.")
    for r in doc.refs:
        p = d.add_paragraph()
        p.add_run(f"[{r.n}] {r.text}")
        if r.url:
            p.add_run(" ")
            _docx_link(p, r.url, url=r.url)
        if r.note:
            p.add_run(f" ({r.note})").italic = True
        _docx_bookmark(p, f"ref{r.n}", ident)
        ident += 1
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _docx_blocks(d, blocks: list[tuple]) -> None:
    for block in blocks:
        kind = block[0]
        if kind == "p":
            _docx_inline(d.add_paragraph(), block[1])
        elif kind in ("ul", "ol"):
            for item in block[1]:
                _docx_inline(d.add_paragraph(style="List Bullet" if kind == "ul" else "List Number"), item)
        elif kind == "quote":
            _docx_inline(d.add_paragraph(style="Quote"), block[1])
        elif kind == "h":
            d.add_heading(re.sub(r"[\x01\x02]", "", block[2]), level=min(max(block[1], 2), 4))
        elif kind == "table":
            rows = block[1]
            width = max(len(r) for r in rows)
            t = d.add_table(rows=len(rows), cols=width)
            t.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci in range(width):
                    cell = t.cell(ri, ci)
                    para = cell.paragraphs[0]
                    _docx_inline(para, row[ci] if ci < len(row) else "")
                    if ri == 0:
                        for run in para.runs:
                            run.bold = True
            d.add_paragraph()


# ------------------------------------------------------------------------------ pdf
_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/Library/Fonts",
    "C:\\Windows\\Fonts",
)


def _register_fonts() -> tuple[str, str, str, str, object | None]:
    """DejaVu (wide Unicode coverage) when installed, else the built-in Helvetica."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for folder in _FONT_DIRS:
        files = {k: os.path.join(folder, v) for k, v in {
            "r": "DejaVuSerif.ttf", "b": "DejaVuSerif-Bold.ttf", "i": "DejaVuSerif-Italic.ttf", "m": "DejaVuSansMono.ttf"}.items()}  # fmt: skip
        if all(os.path.exists(p) for p in files.values()):
            for name, key in (
                ("RPSerif", "r"),
                ("RPSerif-Bold", "b"),
                ("RPSerif-Italic", "i"),
                ("RPMono", "m"),
            ):
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, files[key]))
            pdfmetrics.registerFontFamily(
                "RPSerif",
                normal="RPSerif",
                bold="RPSerif-Bold",
                italic="RPSerif-Italic",
                boldItalic="RPSerif-Bold",
            )
            return "RPSerif", "RPSerif-Bold", "RPSerif-Italic", "RPMono", pdfmetrics.getFont("RPSerif").face
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Courier", None


class _Safe:
    """Escape text for ReportLab paragraph markup and replace characters the font cannot draw."""

    def __init__(self, face):
        self.face, self.lost = face, 0

    def __call__(self, text: str) -> str:
        text = clean(text)
        out = []
        for ch in text:
            ok = ord(ch) < 256 if self.face is None else (ch in "\n\t " or ord(ch) in self.face.charToGlyph)
            if ok:
                out.append(ch)
            else:
                out.append("?")
                self.lost += 1
        return xml_escape("".join(out), {'"': "&quot;"})


def build_pdf(doc: ExportDoc) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        ListFlowable,
        ListItem,
        PageBreak,
        PageTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.platypus.tableofcontents import TableOfContents

    serif, bold, italic, mono, face = _register_fonts()
    safe = _Safe(face)
    link_color = "#0b4f8a"
    base = ParagraphStyle(
        "Body", fontName=serif, fontSize=10.5, leading=15.5, alignment=TA_LEFT, spaceAfter=6
    )
    st = {
        "body": base,
        "title": ParagraphStyle("Title", parent=base, fontName=bold, fontSize=26, leading=32, spaceAfter=14),
        "meta": ParagraphStyle("Meta", parent=base, fontName=italic, textColor=colors.HexColor("#555555")),
        "notice": ParagraphStyle(
            "Notice",
            parent=base,
            fontName=bold,
            textColor=colors.HexColor("#b45309"),
            borderColor=colors.HexColor("#b45309"),
            borderWidth=1,
            borderPadding=6,
            spaceBefore=12,
        ),  # fmt: skip
        "h1": ParagraphStyle(
            "H1", parent=base, fontName=bold, fontSize=16, leading=20, spaceBefore=16, spaceAfter=8
        ),
        "h2": ParagraphStyle(
            "H2", parent=base, fontName=bold, fontSize=12.5, leading=16, spaceBefore=10, spaceAfter=4
        ),
        "quote": ParagraphStyle(
            "Quote", parent=base, fontName=italic, leftIndent=14, textColor=colors.HexColor("#444444")
        ),
        "ref": ParagraphStyle(
            "Ref", parent=base, leftIndent=22, firstLineIndent=-22, fontSize=9.5, leading=13
        ),
        "toc": ParagraphStyle("TOC", parent=base, fontName=serif, fontSize=10.5, leftIndent=8, spaceAfter=3),
    }

    def inline(text: str) -> str:
        out = []
        for kind, value in parse_inline(text):
            v = safe(value)
            if kind == "cite":
                out.append(f'<a href="#ref{value}" color="{link_color}">[{value}]</a>')
            elif kind == "bold":
                out.append(f"<b>{v}</b>")
            elif kind == "italic":
                out.append(f"<i>{v}</i>")
            elif kind == "code":
                out.append(f'<font face="{mono}">{v}</font>')
            else:
                out.append(v)
        return "".join(out)

    class Doc(BaseDocTemplate):
        def afterFlowable(self, flowable):  # noqa: N802 - ReportLab hook
            if isinstance(flowable, Paragraph) and flowable.style.name == "H1":
                key = f"h{self.seq.nextf('h')}"
                self.canv.bookmarkPage(key)
                self.notify("TOCEntry", (0, flowable.getPlainText(), self.page, key))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(serif, 8.5)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawCentredString(A4[0] / 2, 1.2 * cm, f"Page {document.page}")
        canvas.drawString(
            2.2 * cm,
            1.2 * cm,
            safe(doc.title)[:70].replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"),
        )
        canvas.restoreState()

    buf = io.BytesIO()
    pdf = Doc(buf, pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm, topMargin=2.2 * cm, bottomMargin=2.2 * cm,
              title=doc.title[:200], author="ResearchPilot AI")  # fmt: skip
    frame = Frame(pdf.leftMargin, pdf.bottomMargin, pdf.width, pdf.height, id="f")
    pdf.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=footer)])

    flow: list = [Spacer(1, 5 * cm), Paragraph(safe(doc.title), st["title"]),
                  Paragraph(f"Generated {doc.generated.strftime('%d %B %Y, %H:%M')} UTC by ResearchPilot AI", st["meta"])]  # fmt: skip
    if doc.notice:
        flow.append(Paragraph(safe(doc.notice), st["notice"]))
    flow.append(PageBreak())
    if doc.toc:
        toc = TableOfContents()
        toc.levelStyles = [st["toc"]]
        toc.dotsMinLevel = 0
        flow += [Paragraph("Contents", st["h2"]), toc, PageBreak()]
    flow.append(Paragraph("Research scope", st["h1"]))
    for k, v in doc.scope:
        flow.append(Paragraph(f"<b>{safe(k)}:</b> {safe(v)}", st["body"]))
    flow.append(Paragraph("Methodology", st["h1"]))
    flow.append(
        ListFlowable(
            [ListItem(Paragraph(safe(m), st["body"])) for m in doc.methodology],
            bulletType="bullet",
            leftIndent=14,
        )
    )

    def blocks_to_flow(blocks):
        out = []
        for b in blocks:
            if b[0] == "p":
                out.append(Paragraph(inline(b[1]), st["body"]))
            elif b[0] in ("ul", "ol"):
                out.append(ListFlowable([ListItem(Paragraph(inline(x), st["body"])) for x in b[1]],
                                        bulletType="bullet" if b[0] == "ul" else "1", leftIndent=16))  # fmt: skip
            elif b[0] == "quote":
                out.append(Paragraph(inline(b[1]), st["quote"]))
            elif b[0] == "h":
                out.append(Paragraph(inline(b[2]), st["h2"]))
            elif b[0] == "table":
                width = max(len(r) for r in b[1])
                data = [
                    [Paragraph(inline(r[i]) if i < len(r) else "", st["ref"]) for i in range(width)]
                    for r in b[1]
                ]
                t = Table(data, repeatRows=1)
                t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bbbbbb")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]))  # fmt: skip
                out += [t, Spacer(1, 6)]
        return out

    for _, heading, body in doc.sections:
        flow.append(Paragraph(safe(heading), st["h1"]))
        flow += blocks_to_flow(parse_blocks(marked(body, doc)))
    flow.append(Paragraph("Quality assessment", st["h1"]))
    flow.append(
        ListFlowable(
            [ListItem(Paragraph(safe(x), st["body"])) for x in quality_lines(doc)],
            bulletType="bullet",
            leftIndent=14,
        )
    )
    if doc.opinions:
        flow.append(
            Paragraph(
                "AI opinions (rubric-based, not a fact check): "
                + safe(
                    "; ".join(
                        f"{quality.LABELS.get(o.dimension, o.dimension)} {_score_text(o.score)}"
                        for o in doc.opinions
                    )
                ),
                st["body"],
            )
        )
    if doc.warnings:
        flow.append(Paragraph("Warnings", st["h2"]))
        flow.append(
            ListFlowable(
                [ListItem(Paragraph(safe(w["text"]), st["body"])) for w in doc.warnings],
                bulletType="bullet",
                leftIndent=14,
            )
        )
    flow.append(Paragraph("References", st["h1"]))
    if not doc.refs:
        flow.append(Paragraph("No sources were cited.", st["body"]))
    for r in doc.refs:
        link = (
            f' <a href="{xml_escape(r.url, {chr(34): "&quot;"})}" color="{link_color}">{safe(r.url)}</a>'
            if r.url
            else ""
        )
        note = f" <i>({safe(r.note)})</i>" if r.note else ""
        flow.append(Paragraph(f'<a name="ref{r.n}"/>[{r.n}] {safe(r.text)}{link}{note}', st["ref"]))
    if safe.lost:
        flow.append(
            Paragraph(
                f"Note: {safe.lost} character(s) could not be drawn with the PDF font and were replaced by '?'. "
                "Use the Word or HTML export for full language support.",
                st["meta"],
            )
        )
    pdf.multiBuild(flow)
    return buf.getvalue()


# ------------------------------------------------------------------------------ entry point
def export(report: ReportDocument, fmt: str) -> tuple[bytes, str, str]:
    """Return (content, content type, file extension)."""
    if fmt not in FORMATS:
        raise ValueError(fmt)
    doc = build_model(report)
    builder = {"md": build_markdown, "html": build_html, "docx": build_docx, "pdf": build_pdf}[fmt]
    content_type, ext = FORMATS[fmt]
    return builder(doc), content_type, ext


def filename_for(report: ReportDocument, ext: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", report.title).strip("-")[:60] or "report"
    return f"{slug}-{utcnow().strftime('%Y%m%d')}.{ext}"
