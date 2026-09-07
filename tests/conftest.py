"""Shared test fixtures. First arrival: fabricating SCANNED PDFs — pages that are pure pixels with no
text layer, exactly what a office scanner produces — so the OCR path can be tested end to end."""
from __future__ import annotations

import io
from pathlib import Path

import pytest


@pytest.fixture
def make_scanned_pdf(tmp_path: Path):
    """Factory: make_scanned_pdf('name.pdf', ['page one text', 'page two text']) → Path.

    Each string becomes one PAGE whose text exists only as rendered pixels (drawn onto a JPEG that
    fills the page), so pypdf's text layer is empty and only OCR can read it. Big clean type at
    ~200 DPI — testing the plumbing, not the engine's eyesight."""
    PIL = pytest.importorskip("PIL.Image")
    draw_mod = pytest.importorskip("PIL.ImageDraw")
    font_mod = pytest.importorskip("PIL.ImageFont")
    pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    def _make(name: str, page_texts: list[str]) -> Path:
        out = tmp_path / name
        c = canvas.Canvas(str(out), pagesize=letter)
        for text in page_texts:
            img = PIL.new("RGB", (1700, 2200), "white")
            d = draw_mod.Draw(img)
            try:
                font = font_mod.truetype("arial.ttf", 48)
            except OSError:
                font = font_mod.load_default()
            y = 200
            for line in text.split("\n"):
                d.text((150, y), line, fill="black", font=font)
                y += 90
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 0, 0,
                        width=letter[0], height=letter[1])
            c.showPage()
        c.save()
        return out

    return _make
