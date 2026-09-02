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


def _mixed_typed_and_blank_layer_pdf(tmp_path):
    """A contract shaped like the real ones: a typed first page, then a page whose text layer is empty
    because it is a scan of a signature. Built with reportlab alone so it needs no OCR engine."""
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    p = tmp_path / "contract.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 720, "Deliverables are due within thirty days of award.")
    c.showPage()
    c.showPage()            # page two carries no text layer at all — pixels, as far as pypdf can tell
    c.save()
    return p


def test_a_mixed_contract_says_so_when_ocr_fails_outright(tmp_path, monkeypatch):
    # OCR present but returning nothing at all is a FAILURE, not a cap. The typed pages still come
    # through — the danger is the model reasoning over a contract whose signature page vanished.
    from helix.services import ocr

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "scan_pdf_pages", lambda *a, **k: {})
    text = doc_extract.extract(_mixed_typed_and_blank_layer_pdf(tmp_path))
    assert "Deliverables are due within thirty days" in text     # what was read is still read
    assert "1 scanned page(s) could not be read" in text         # and what was lost is said out loud


def _typed_page_then_two_blank_layers(tmp_path):
    """A typed first page followed by two pages with no text layer at all — the shape of a contract
    whose exhibits are scans. Needs no OCR engine: reportlab alone produces the empty text layers."""
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    p = tmp_path / "exhibits.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 720, "Deliverables are due within thirty days of award.")
    c.showPage()
    c.showPage()
    c.showPage()
    c.save()
    return p


def test_a_page_that_errored_is_not_blamed_on_the_ocr_caps(tmp_path, monkeypatch):
    # ocr._scan guards every page individually now, so ONE page tripping pdfium leaves its index out
    # of the returned dict while its neighbours come back read — a shape that could not happen before
    # that guard landed, and which lands in exactly the same "missed" list the caps fill. With two
    # suspected scans and a cap of a hundred, nothing was rationed; saying "OCR page/time limit" would
    # hand the user, and the model reading this text, a cause that never applied.
    from helix.services import ocr

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "scan_pdf_pages",
                        lambda path, idxs, **k: {idxs[0]: "EXHIBIT A — SIGNATURE ON FILE"})
    text = doc_extract.extract(_typed_page_then_two_blank_layers(tmp_path))
    assert "EXHIBIT A" in text                                  # the page that did read is kept
    assert "1 more scanned page(s) not transcribed" in text     # the one that didn't is still said
    assert "limit" not in text and "cap" not in text, (
        "a page that errored is being reported as rationed away by the page/time caps"
    )


def test_the_page_cap_is_still_named_when_it_actually_bit(tmp_path, monkeypatch):
    # The other half of the same choice: when there genuinely were more suspected scans than
    # _OCR_MAX_PAGES, the cap DID bite (scan_pdf_pages never looks past that slice), and a user with a
    # long scan deserves to know the document was too long rather than that it was unreadable.
    from helix.services import ocr

    monkeypatch.setattr(doc_extract, "_OCR_MAX_PAGES", 1)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "scan_pdf_pages",
                        lambda path, idxs, *, max_pages, budget_s: {idxs[0]: "EXHIBIT A — PAGE ONE"})
    text = doc_extract.extract(_typed_page_then_two_blank_layers(tmp_path))
    assert "1 more scanned page(s) not transcribed" in text
    assert "page limit" in text


def test_a_typed_pdf_with_short_pages_is_never_accused_of_hiding_scans(tmp_path, monkeypatch):
    # Pages under the scan threshold are only a SUSPICION — a cover sheet is short and fully typed.
    # If OCR fails on one of those, saying "pages could not be read" would be a lie to the model.
    from helix.services import ocr

    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    p = tmp_path / "short.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 720, "Statement of Work")          # 17 chars — under _SCAN_TEXT_MIN
    c.showPage()
    c.drawString(72, 720, "Period of performance: 12 months.")   # 33 — also under it
    c.save()
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "scan_pdf_pages", lambda *a, **k: {})
    text = doc_extract.extract(p)
    assert "Statement of Work" in text
    assert "could not be read" not in text and "not transcribed" not in text
