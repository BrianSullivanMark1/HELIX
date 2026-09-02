"""ocr — reading scanned PDF pages from their pixels via pdfium + Windows.Media.Ocr, with graceful
degradation when either half is missing. Engine-dependent tests skip on a machine without OCR."""
from __future__ import annotations

import pytest

from helix.services import ocr

needs_engine = pytest.mark.skipif(not ocr.available(), reason="Windows OCR engine unavailable")


@needs_engine
def test_scan_reads_text_out_of_pixels(make_scanned_pdf):
    pdf = make_scanned_pdf("scan.pdf", ["PERFORMANCE WORK STATEMENT\nContract N00164-26-R-0001"])
    out = ocr.scan_pdf_pages(pdf, [0], max_pages=10, budget_s=30.0)
    assert 0 in out
    assert "PERFORMANCE WORK STATEMENT" in out[0]
    # The serial survives modulo the classic O/0 and I/1 lookalikes (engine traits, not plumbing bugs).
    trans = str.maketrans({"O": "0", "I": "1", "l": "1", " ": None})
    assert "N00164-26-R-0001" in out[0].translate(trans)


@needs_engine
def test_scan_respects_the_page_cap(make_scanned_pdf):
    pdf = make_scanned_pdf("two.pdf", ["FIRST PAGE ALPHA", "SECOND PAGE BRAVO"])
    out = ocr.scan_pdf_pages(pdf, [0, 1], max_pages=1, budget_s=30.0)
    assert list(out) == [0]  # only the first requested page was attempted
    assert "ALPHA" in out[0]


@needs_engine
def test_scan_skips_out_of_range_pages(make_scanned_pdf):
    pdf = make_scanned_pdf("one.pdf", ["ONLY PAGE"])
    out = ocr.scan_pdf_pages(pdf, [0, 7], max_pages=10, budget_s=30.0)
    assert 7 not in out and 0 in out


def test_scan_never_raises_on_garbage(tmp_path):
    bad = tmp_path / "not_really.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not a pdf at all")
    assert ocr.scan_pdf_pages(bad, [0], max_pages=5, budget_s=5.0) == {}
    assert ocr.scan_pdf_pages(tmp_path / "missing.pdf", [0], max_pages=5, budget_s=5.0) == {}


def test_empty_requests_short_circuit(tmp_path):
    # No indices / zero cap must not even try to open the document.
    assert ocr.scan_pdf_pages(tmp_path / "whatever.pdf", [], max_pages=5, budget_s=5.0) == {}
    assert ocr.scan_pdf_pages(tmp_path / "whatever.pdf", [0], max_pages=0, budget_s=5.0) == {}


def test_one_failing_page_does_not_discard_the_pages_already_read(make_scanned_pdf, monkeypatch):
    """A 90-page scan must not come back empty because page 44 was malformed. Renders for real
    (pdfium) but stubs the engine, so this pins the loop's tolerance on any machine."""
    pytest.importorskip("pypdfium2")
    pdf = make_scanned_pdf("three.pdf", ["PAGE ONE", "PAGE TWO", "PAGE THREE"])
    monkeypatch.setattr(ocr, "_available", True)          # skip the WinRT probe
    monkeypatch.setattr(ocr, "_make_engine", lambda: object())
    seen: list[int] = []

    async def fake_recognize(engine, bgra):
        seen.append(len(seen))
        if len(seen) == 2:                                # the second page is the bad one
            raise RuntimeError("bitmap went sideways")
        return f"READ {len(seen)}"

    monkeypatch.setattr(ocr, "_recognize", fake_recognize)
    out = ocr.scan_pdf_pages(pdf, [0, 1, 2], max_pages=10, budget_s=30.0)
    assert sorted(out) == [0, 2]        # the good pages survived the bad one…
    assert 1 not in out                 # …and the failed page is ABSENT, not a "" that reads as blank
    assert len(seen) == 3               # the loop kept going rather than unwinding at page two
