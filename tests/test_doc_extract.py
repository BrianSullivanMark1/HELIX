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


def test_extract_is_graceful_on_bad_or_missing(tmp_path):
    # A corrupt PDF / DOCX must yield "" rather than raising into the ingest loop.
    bad_pdf = tmp_path / "broken.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4 not actually a pdf")
    assert doc_extract.extract(bad_pdf) == ""
    assert doc_extract.extract(tmp_path / "missing.docx") == ""
    assert doc_extract.extract(tmp_path / "note.txt") == ""  # not a rich doc → handled by the caller
