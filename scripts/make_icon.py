"""Generate the HELIX app icon — the Presence orb, as a multi-size Windows .ico.

A glowing cyan core with a bright center and an amber accent arc, on a dark rounded tile, matching the
in-app PresenceOrb. Run:  python scripts/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

S = 1024
CX = CY = S / 2
ASSETS = Path(__file__).resolve().parent.parent / "assets"

# field of radial distances from center (for gradient disks)
_yy, _xx = np.mgrid[0:S, 0:S].astype(np.float32)
_RR = np.sqrt((_xx - CX) ** 2 + (_yy - CY) ** 2)


def disk(radius: float, color: tuple[int, int, int], falloff: float, max_alpha: int) -> Image.Image:
    """A soft radial disk: full color at center fading to transparent at `radius`."""
    t = np.clip(_RR / radius, 0, 1)
    alpha = ((1 - t) ** falloff * max_alpha).astype(np.uint8)
    arr = np.zeros((S, S, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3] = color[0], color[1], color[2], alpha
    return Image.fromarray(arr, "RGBA")


def build() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # dark rounded tile (matches the app background)
    ImageDraw.Draw(img).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=(9, 12, 16, 255)
    )

    # outer glow
    glow = disk(S * 0.44, (63, 224, 224), falloff=2.4, max_alpha=140).filter(
        ImageFilter.GaussianBlur(S * 0.05)
    )
    img.alpha_composite(glow)

    # core — three stops: dim cyan edge -> cyan -> near-white center
    img.alpha_composite(disk(S * 0.275, (29, 107, 107), 1.2, 255))
    img.alpha_composite(disk(S * 0.205, (63, 224, 224), 1.4, 255))
    img.alpha_composite(disk(S * 0.115, (234, 255, 255), 1.7, 255))

    # amber accent arc (the 'thinking' ring), softly glowing
    arc = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    m = S * 0.305
    ImageDraw.Draw(arc).arc(
        [CX - m, CY - m, CX + m, CY + m], start=-58, end=72,
        fill=(245, 166, 35, 240), width=int(S * 0.020),
    )
    img.alpha_composite(arc.filter(ImageFilter.GaussianBlur(S * 0.004)))
    return img


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    img = build()
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(ASSETS / "helix.ico", format="ICO", sizes=sizes)
    img.resize((512, 512), Image.LANCZOS).save(ASSETS / "helix_icon.png")
    print("wrote", ASSETS / "helix.ico", "and helix_icon.png")


if __name__ == "__main__":
    main()
