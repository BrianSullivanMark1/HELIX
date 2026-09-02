"""Read the TEXT out of scanned PDF pages — pixels in, words out, fully on-machine.

A scanned PDF has no text layer; pypdf hands back nothing and the document is a brick. This module
renders those pages to bitmaps (pypdfium2 — BSD-licensed, bundles its own pdfium) and reads them with
Windows.Media.Ocr — the OCR engine Windows itself ships. That engine was chosen deliberately over the
pip-installable alternatives: it is ~100x faster than the onnx OCR stacks measured on this machine
(~0.1s/page vs ~9s/page), it is trained for English so word spacing survives, and it adds nothing to
the frozen build but thin bindings. It also matches the house ethos (mediasense's raw WASAPI): the
document never leaves the machine — same promise faster-whisper makes for audio.

Everything is lazy and best-effort: missing libraries, a non-Windows host, or a machine with no OCR
language pack all degrade to "OCR unavailable" — callers fall back to whatever text layer exists.
Nothing here ever raises. pdfium is not thread-safe, so rendering is serialized module-wide.
"""
from __future__ import annotations

import threading
import time

from helix.logging_setup import get_logger

_LOG = get_logger("ocr")

_DPI = 200                # digits survive at 200 (150 read '0001' as 'OOOI' in testing); cheap at ~0.15s/page
_MAX_SIDE_PX = 4_000      # clamp a giant page (engine limit is 10k; 4k is plenty for text)
_PDFIUM_LOCK = threading.Lock()  # pdfium is explicitly not thread-safe — serialize all rendering

_available: bool | None = None  # tri-state probe cache


def available() -> bool:
    """True when both halves exist: pypdfium2 to render, and a Windows OCR engine for SOME language.
    Probed once and cached — the answer can't change mid-run."""
    global _available
    if _available is None:
        try:
            import pypdfium2  # noqa: F401
            _available = _make_engine() is not None
        except Exception as exc:  # noqa: BLE001 — any import/COM failure just means "no OCR"
            _LOG.info("OCR unavailable: %s", exc)
            _available = False
    return _available


def _make_engine():
    """A fresh OcrEngine for the calling thread (creation is ~ms; no cross-thread COM sharing)."""
    from winsdk.windows.globalization import Language
    from winsdk.windows.media.ocr import OcrEngine

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        engine = OcrEngine.try_create_from_language(Language("en-US"))
    return engine


def scan_pdf_pages(path, indices: list[int], *, max_pages: int, budget_s: float) -> dict[int, str]:
    """OCR the given zero-based pages of a PDF → {index: text}. Stops early at max_pages or when the
    wall-clock budget runs out (the caller reports what was left untranscribed). A page that renders
    blank maps to ""; a page that FAILS is left out of the dict entirely, so the caller counts it as
    untranscribed and says so. Returns {} when OCR is unavailable or the document won't open."""
    if not indices or max_pages <= 0 or not available():
        return {}
    try:
        return _scan(path, indices[:max_pages], budget_s)
    except Exception as exc:  # noqa: BLE001 — a bad document must never break an ingest/attach turn
        _LOG.warning("OCR failed for %s: %s", path, exc)
        return {}


def _scan(path, indices: list[int], budget_s: float) -> dict[int, str]:
    import asyncio

    import numpy as np
    import pypdfium2 as pdfium

    out: dict[int, str] = {}
    deadline = time.monotonic() + budget_s

    async def run() -> None:
        engine = _make_engine()
        if engine is None:
            return
        doc = pdfium.PdfDocument(str(path))
        try:
            n = len(doc)
            for idx in indices:
                if idx >= n or time.monotonic() > deadline:
                    break
                # Guard EVERY page individually. Without this, one corrupt page object, one render that
                # trips pdfium, or one WinRT hiccup inside _recognize unwound the whole loop before
                # `return out` and the catch-all above handed back {} — a 90-page scan came back empty
                # because of page 44, and the caller, seeing nothing read at all, couldn't even tell
                # anyone pages were lost. Now the pages already transcribed survive the bad one.
                try:
                    page = doc[idx]
                    w_pt, h_pt = page.get_size()
                    scale = min(_DPI / 72, _MAX_SIDE_PX / max(w_pt, h_pt, 1.0))
                    arr = page.render(scale=scale).to_numpy()
                    if arr.shape[2] == 3:  # pdfium renders BGR; the engine wants BGRA — pad opaque alpha
                        h, w, _ = arr.shape
                        arr = np.dstack([arr, np.full((h, w, 1), 255, np.uint8)])
                    out[idx] = await _recognize(engine, np.ascontiguousarray(arr))
                except Exception as exc:  # noqa: BLE001 — one bad page must not cost the other 89
                    # Deliberately leave `idx` ABSENT rather than storing "": an empty string reads as a
                    # genuinely blank page, counts as "read" to the caller, and the page then disappears
                    # without a word. An absent index lands in the caller's "missed" list, which is what
                    # gets said out loud in the extracted text.
                    _LOG.warning("OCR failed on page %d of %s: %s", idx, path, exc)
        finally:
            doc.close()

    with _PDFIUM_LOCK:
        asyncio.run(run())
    return out


async def _recognize(engine, bgra) -> str:
    """One bitmap through the Windows engine → its lines of text, top to bottom."""
    from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winsdk.windows.security.cryptography import CryptographicBuffer

    h, w = bgra.shape[:2]
    buf = CryptographicBuffer.create_from_byte_array(bgra.tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(buf, BitmapPixelFormat.BGRA8, w, h)
    result = await engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines).strip()
