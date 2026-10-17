"""Reading uploaded PDF, DOCX and TXT files.

Files are validated by content (magic bytes / structure), not by file name or the browser's
content type. Nothing is written to disk: text is extracted from memory and only the text is kept.
Documents are untrusted input, so limits apply everywhere (pages, uncompressed size, characters).
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePath

from charset_normalizer import from_bytes

from research_tools.untrusted import sanitize_text

ALLOWED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "txt"}
MAX_PDF_PAGES = 300
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_DOCX_MEMBERS = 2000
MAX_TEXT_CHARS = 500_000


class DocumentError(ValueError):
    """The file cannot be accepted; the message is safe to show to the user."""


@dataclass
class ParsedDocument:
    kind: str
    filename: str
    text: str
    sha256: str
    size: int
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False


def safe_filename(name: str) -> str:
    """Display name only (files are never stored by name): no path, no control characters."""
    base = PurePath((name or "").replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip().strip(".")
    return (base or "document")[:200]


def detect_kind(filename: str, data: bytes) -> str:
    ext = PurePath(filename.lower()).suffix
    expected = ALLOWED_EXTENSIONS.get(ext)
    if expected is None:
        raise DocumentError("Only PDF, DOCX, TXT and MD files are supported.")
    if not data:
        raise DocumentError("The file is empty.")
    if expected == "pdf":
        if not data.startswith(b"%PDF-"):
            raise DocumentError("The file is not a valid PDF.")
    elif expected == "docx":
        if not data.startswith(b"PK"):
            raise DocumentError("The file is not a valid DOCX document.")
    else:
        if b"\x00" in data[:8192] or data.startswith((b"%PDF-", b"PK\x03\x04")):
            raise DocumentError("The file does not look like plain text.")
    return expected


def _read_pdf(data: bytes) -> tuple[str, int, list[str]]:
    import fitz  # PyMuPDF

    warnings: list[str] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - library raises many types for bad files
        raise DocumentError("The PDF could not be read (it may be damaged).") from exc
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise DocumentError("Password-protected PDFs are not supported.")
        pages = doc.page_count
        if pages > MAX_PDF_PAGES:
            warnings.append(f"only the first {MAX_PDF_PAGES} of {pages} pages were read")
        parts = []
        total = 0
        for index in range(min(pages, MAX_PDF_PAGES)):
            page_text = doc.load_page(index).get_text("text") or ""
            parts.append(f"[page {index + 1}]\n{page_text}")
            total += len(page_text)
            if total > MAX_TEXT_CHARS * 2:
                warnings.append("text limit reached; the rest was not read")
                break
        text = "\n\n".join(parts)
        if len(re.sub(r"\[page \d+\]|\s", "", text)) < 20:
            warnings.append("no readable text found (scanned images are not OCR'd)")
        return text, pages, warnings
    finally:
        doc.close()


def _read_docx(data: bytes) -> tuple[str, list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentError("The file is not a valid DOCX document.") from exc
    with archive:
        members = archive.infolist()
        if len(members) > MAX_DOCX_MEMBERS or sum(m.file_size for m in members) > MAX_DOCX_UNCOMPRESSED:
            raise DocumentError("The DOCX file is too large when unpacked.")
        if any(m.filename.startswith(("/", "..")) or ".." in m.filename.split("/") for m in members):
            raise DocumentError("The DOCX file contains unsafe paths.")
        if "word/document.xml" not in archive.namelist():
            raise DocumentError("The file is not a valid DOCX document.")
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DocumentError("The DOCX file could not be read.") from exc
    lines = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines), []


def _read_text(data: bytes) -> str:
    best = from_bytes(data[:500_000]).best()
    if best is None:
        raise DocumentError("The text file could not be decoded.")
    return data.decode(best.encoding, errors="replace")


def parse_document(filename: str, data: bytes, *, max_bytes: int = 10 * 1024 * 1024) -> ParsedDocument:
    name = safe_filename(filename)
    if len(data) > max_bytes:
        raise DocumentError(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    kind = detect_kind(name, data)
    pages = None
    warnings: list[str] = []
    if kind == "pdf":
        raw, pages, warnings = _read_pdf(data)
    elif kind == "docx":
        raw, warnings = _read_docx(data)
    else:
        raw = _read_text(data)
    text = sanitize_text(raw, MAX_TEXT_CHARS + 1)
    truncated = len(text) > MAX_TEXT_CHARS
    text = text[:MAX_TEXT_CHARS]
    if not re.sub(r"\[page \d+\]", "", text).strip():
        raise DocumentError("No readable text was found in the file.")
    return ParsedDocument(
        kind=kind, filename=name, text=text, sha256=hashlib.sha256(data).hexdigest(), size=len(data),
        page_count=pages, warnings=warnings + (["text was truncated"] if truncated else []), truncated=truncated,
    )  # fmt: skip
