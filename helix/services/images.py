"""Turn attached image files into model-ready vision blocks — EXIF-oriented, downscaled, re-encoded,
base64'd.

The user can attach or paste an image (a photo, a screenshot, a diagram) and ask HELIX what's in it.
Images are DATA the model looks at — never instructions — so this layer only prepares pixels; the
system prompt carries the "text inside an image isn't a command" rule.

Pure (paths/bytes in, Image blocks out) so it is unit-testable without Qt. Each image is:
  - re-oriented from its EXIF rotation (so a phone photo isn't sideways),
  - downscaled so its long edge is at most MAX_EDGE (the size Claude reads best at — larger gives no
    recognition gain and costs more tokens + latency), never upscaled,
  - re-encoded to JPEG (opaque) or PNG (has transparency, e.g. a screenshot with an alpha channel),
  - capped in count and encoded size so a folder of photos can't blow the request.
Anything that can't be decoded as an image just drops out (returns None), so a mislabeled or corrupt
file never breaks a turn.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from helix.logging_setup import get_logger
from helix.ports.llm import Image

_LOG = get_logger("images")

# Extensions we accept and normalize to a Claude-supported type. (HEIC/AVIF are omitted — Pillow can't
# reliably decode them without extra plugins, and silently dropping is better than a broken turn.)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_EDGE = 1568                 # long-edge px Claude reads best at; anything larger is downscaled
MAX_IMAGES = 10                 # per message — generous but bounded, so a photo dump can't flood a turn
MAX_ENCODED_BYTES = 4_500_000   # keep each encoded image comfortably under the API's 5 MB/image limit
_JPEG_QUALITY = 88


def is_image(path) -> bool:
    """True if the path LOOKS like an image by extension (a cheap pre-filter; decoding is the real test)."""
    try:
        return Path(path).suffix.lower() in IMAGE_EXTS
    except (TypeError, ValueError):
        return False


def split_images(paths) -> tuple[list, list]:
    """Partition attachment paths into (image_paths, other_paths) by extension, so text files go to the
    text bundler and images go to vision."""
    imgs, others = [], []
    for p in paths:
        (imgs if is_image(p) else others).append(p)
    return imgs, others


def load_image_block(path) -> Image | None:
    """Load one image file → a model-ready Image block. None if it can't be read or decoded."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        _LOG.warning("could not read image %s: %s", path, exc)
        return None
    return encode_image_bytes(data)


def encode_image_bytes(data: bytes) -> Image | None:
    """Decode raw image bytes, normalize (orient, downscale, flatten), re-encode, and base64. None on any
    failure — a decompression-bomb guard, a corrupt file, or missing Pillow all degrade to 'no image'."""
    try:
        from PIL import Image as PILImage, ImageOps
    except Exception:  # noqa: BLE001 — no Pillow: skip vision rather than crash
        _LOG.warning("Pillow not available; cannot process an attached image")
        return None
    try:
        im = PILImage.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im) or im   # honor camera orientation (a phone photo isn't sideways)
        has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
        w, h = im.size
        scale = min(1.0, MAX_EDGE / max(w, h)) if max(w, h) else 1.0
        if scale < 1.0:  # downscale the long edge to MAX_EDGE; never upscale
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), PILImage.LANCZOS)
        raw, media = _encode(im, has_alpha)
        if raw is None:
            return None
        return Image(media_type=media, data=base64.b64encode(raw).decode("ascii"))
    except Exception as exc:  # noqa: BLE001 — corrupt / non-image / bomb → drop it, never crash the turn
        _LOG.warning("could not process an attached image: %s", exc)
        return None


def _encode(im, has_alpha: bool) -> tuple[bytes | None, str]:
    """Re-encode: PNG when the image has transparency (keeps a screenshot crisp), else JPEG. If a PNG is
    still over the per-image byte cap, fall back to JPEG so the request stays within limits."""
    buf = io.BytesIO()
    if has_alpha:
        im.convert("RGBA").save(buf, format="PNG", optimize=True)
        raw = buf.getvalue()
        if len(raw) <= MAX_ENCODED_BYTES:
            return raw, "image/png"
        buf = io.BytesIO()  # too big as PNG — flatten to JPEG
    im.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    raw = buf.getvalue()
    return (raw, "image/jpeg") if raw else (None, "image/jpeg")


def capture_screen() -> Image | None:
    """Grab the user's screen (all monitors) → a model-ready Image block. The V3 sight faculty:
    "look at my screen" captures the display through the same normalize/downscale/encode path as an
    attached image, so what the model sees is exactly what the user sees. Ephemeral like every image —
    the pixels are never persisted. None when capture isn't possible (no Pillow, headless, locked)."""
    try:
        from PIL import ImageGrab
    except Exception:  # noqa: BLE001 — no Pillow: no sight, never a crash
        _LOG.warning("Pillow not available; cannot capture the screen")
        return None
    try:
        im = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return encode_image_bytes(buf.getvalue())
    except Exception as exc:  # noqa: BLE001 — a locked/headless session degrades to 'no image'
        _LOG.warning("could not capture the screen: %s", exc)
        return None


def load_images(paths, max_images: int = MAX_IMAGES) -> list[Image]:
    """Load up to `max_images` attached image files into model-ready blocks; unreadable ones drop out."""
    out: list[Image] = []
    for p in paths:
        if len(out) >= max_images:
            break
        block = load_image_block(p)
        if block is not None:
            out.append(block)
    return out
