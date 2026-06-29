"""Extract plain text from a document so it can be ingested into a knowledge base.

Plain-text/markup files are read by the caller; this module handles the RICH formats — PDF and Word —
via optional libraries (pypdf / python-docx) imported LAZILY, so a missing library degrades to "this
format isn't supported right now" instead of crashing the app. Everything is best-effort: a corrupt or
encrypted file yields an empty string, never an exception into the ingest loop.
"""
from __future__ import annotations

from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("doc_extract")

PDF_EXT = ".pdf"
DOCX_EXT = ".docx"
RICH_EXT = frozenset({PDF_EXT, DOCX_EXT})


def is_rich_doc(path) -> bool:
    """True for a format this module extracts (PDF / Word) rather than plain text."""
    return Path(path).suffix.lower() in RICH_EXT


def extract(path) -> str:
    """The text content of a PDF or Word document, or "" if unsupported, unreadable, or the optional
    library isn't installed. Never raises."""
    suffix = Path(path).suffix.lower()
    try:
        if suffix == PDF_EXT:
            return _extract_pdf(path)
        if suffix == DOCX_EXT:
            return _extract_docx(path)
    except Exception as exc:  # noqa: BLE001 - a bad document must never break the ingest loop
        _LOG.warning("could not extract %s: %s", path, exc)
    return ""


def _extract_pdf(path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        _LOG.info("pypdf not installed — PDF ingestion unavailable")
        return ""
    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")  # try an empty-password decrypt; bail quietly if it won't open
        except Exception:  # noqa: BLE001
            return ""
    parts = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_docx(path) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        _LOG.info("python-docx not installed — Word ingestion unavailable")
        return ""
    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    # Include table cell text too — a lot of Word content lives in tables.
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append("\t".join(cells))
    return "\n".join(parts).strip()
