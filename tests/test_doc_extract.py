"""doc_extract — pulling text from PDF / Word documents, with graceful failure when a file is bad or a
library is missing."""
from __future__ import annotations

import pytest

from helix.services import doc_extract


def test_is_rich_doc_detection():
    assert doc_extract.is_rich_doc("report.pdf")
    assert doc_extract.is_rich_doc("Memo.DOCX")
    assert not doc_extract.is_rich_doc("notes.txt")
    assert not doc_extract.is_rich_doc("data.csv")


def test_extract_pdf_round_trips(tmp_path):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    p = tmp_path / "report.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 720, "Statement of Work")
    c.showPage()
    c.drawString(72, 720, "Period of performance: 12 months.")
    c.save()
    text = doc_extract.extract(p)
    assert "Statement of Work" in text
    assert "Period of performance: 12 months." in text  # page two is included, not just the first


def test_extract_docx_round_trips(tmp_path):
    docx = pytest.importorskip("docx")  # python-docx
    p = tmp_path / "memo.docx"
    d = docx.Document()
    d.add_paragraph("Project kickoff is Monday.")
    d.add_paragraph("Bring the deck.")
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Owner"
    table.rows[0].cells[1].text = "Dana"
    d.save(str(p))
    text = doc_extract.extract(p)
    assert "Project kickoff is Monday." in text
    assert "Bring the deck." in text
    assert "Owner" in text and "Dana" in text  # table cells are included


def test_extract_reads_a_scanned_pdf_via_ocr(make_scanned_pdf):
    from helix.services import ocr
    if not ocr.available():
        pytest.skip("Windows OCR engine unavailable")
    pdf = make_scanned_pdf("scanned.pdf", ["THE QUICK BROWN FOX\nJUMPS OVER THE LAZY DOG"])
    text = doc_extract.extract(pdf)
    assert "QUICK BROWN FOX" in text
    assert "LAZY DOG" in text


def test_extract_merges_text_layer_and_ocr_per_page(make_scanned_pdf, tmp_path):
    # A MIXED document: page 1 has a real text layer, page 2 is pure pixels. Both must come through —
    # the scan decision is per page, not per document.
    from helix.services import ocr
    if not ocr.available():
        pytest.skip("Windows OCR engine unavailable")
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    from pypdf import PdfReader, PdfWriter

    typed = tmp_path / "typed.pdf"
    c = canvas.Canvas(str(typed))
    c.drawString(72, 720, "Typed page: deliverables are due in thirty days.")
    c.save()
    scanned = make_scanned_pdf("scan_part.pdf", ["SCANNED PAGE: SIGNATURE ON FILE"])

    mixed = tmp_path / "mixed.pdf"
    w = PdfWriter()
    for src in (typed, scanned):
        for page in PdfReader(str(src)).pages:
            w.add_page(page)
    with open(mixed, "wb") as fh:
        w.write(fh)

    text = doc_extract.extract(mixed)
    assert "deliverables are due in thirty days" in text   # text layer kept (never re-OCR'd worse)
    assert "SIGNATURE ON FILE" in text                     # scanned page transcribed


def test_extract_still_returns_text_layer_when_ocr_is_unavailable(tmp_path, monkeypatch):
    # No OCR on the machine → a typed PDF must read exactly as before (the fallback never subtracts).
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    from helix.services import ocr

    p = tmp_path / "typed.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 720, "Plain typed content survives.")
    c.save()
    monkeypatch.setattr(ocr, "available", lambda: False)
    assert "Plain typed content survives." in doc_extract.extract(p)


def test_extract_notes_pages_the_ocr_caps_left_behind(make_scanned_pdf, monkeypatch):
    from helix.services import ocr
    if not ocr.available():
        pytest.skip("Windows OCR engine unavailable")
    pdf = make_scanned_pdf("long_scan.pdf", ["PAGE ONE CONTENT", "PAGE TWO CONTENT", "PAGE THREE CONTENT"])
    monkeypatch.setattr(doc_extract, "_OCR_MAX_PAGES", 1)
    text = doc_extract.extract(pdf)
    assert "PAGE ONE CONTENT" in text
    assert "2 more scanned page(s) not transcribed" in text  # honesty over silent omission


def test_extract_is_graceful_on_bad_or_missing(tmp_path):
    # A corrupt PDF / DOCX must yield "" rather than raising into the ingest loop.
    bad_pdf = tmp_path / "broken.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4 not actually a pdf")
    assert doc_extract.extract(bad_pdf) == ""
    assert doc_extract.extract(tmp_path / "missing.docx") == ""
    assert doc_extract.extract(tmp_path / "note.txt") == ""  # not a rich doc → handled by the caller
