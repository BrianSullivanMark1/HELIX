"""Generate the HELIX app icon — the in-app electronic Presence orb — as a multi-size Windows .ico.

Renders the EXACT same orb the app draws (helix.ui.orb.paint_orb), large and centred on a transparent
background (no dark tile), so the taskbar icon matches the app. Run:  python scripts/make_icon.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so 'helix' imports when run directly

from PIL import Image
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPainter

from helix.ui.orb import _build_circuits, _build_smoke, paint_orb

S = 1024
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def render(size: int) -> Image.Image:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(0)  # transparent — the orb floats, no black background tile
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    rings, traces, nodes = _build_circuits()
    paint_orb(
        p, size / 2, size / 2, size * 0.36,  # centred; sphere ~72% of the frame
        warm=0.0, glow=0.9, t=120.0,         # cyan, bright, a nice static frame
        rings=rings, traces=traces, nodes=nodes, smoke=_build_smoke(),
    )
    p.end()
    rgba = img.convertToFormat(QImage.Format.Format_RGBA8888)
    ptr = rgba.constBits()
    ptr.setsize(rgba.sizeInBytes())
    return Image.frombuffer("RGBA", (size, size), bytes(ptr), "raw", "RGBA", rgba.bytesPerLine(), 1)


def main() -> None:
    _app = QGuiApplication([])  # required for QPainter/QImage
    ASSETS.mkdir(parents=True, exist_ok=True)
    big = render(S)
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    big.save(ASSETS / "helix.ico", format="ICO", sizes=sizes)
    big.resize((512, 512), Image.LANCZOS).save(ASSETS / "helix_icon.png")
    print("wrote", ASSETS / "helix.ico", "and helix_icon.png")


if __name__ == "__main__":
    main()
