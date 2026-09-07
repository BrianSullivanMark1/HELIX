"""Generate assets/orb.ico — a glowing 'Contained Star' orb, matching the app's presence orb.

A cyan-white core folding to deep teal-blue at the limb, a soft outer glow, a bright fresnel rim,
and one faint orbital ring — rendered at 4x and downsampled, saved as a multi-resolution Windows
icon (256…16). Pure numpy + Pillow. Run: python assets/make_orb_icon.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

SS = 1024                      # supersample canvas, downsampled into each icon size
OUT = Path(__file__).resolve().parent / "orb.ico"


def _clip(a):
    return np.clip(a, 0.0, 1.0)


def render(n: int) -> Image.Image:
    y, x = np.mgrid[0:n, 0:n].astype(np.float64)
    cx = cy = (n - 1) / 2.0
    R = n * 0.40                       # sphere radius (fills the icon; glow lives in the margin)
    dx = (x - cx) / R
    dy = (y - cy) / R
    r2 = dx * dx + dy * dy
    r = np.sqrt(r2)

    rgb = np.zeros((n, n, 3), dtype=np.float64)
    alpha = np.zeros((n, n), dtype=np.float64)

    # ---- outer glow: a TIGHT cyan halo hugging the rim (not a square-filling haze) ----
    glow = np.exp(-np.maximum(r - 1.0, 0.0) * 7.5)
    glow_col = np.array([0.16, 0.85, 0.95])
    rgb += glow_col[None, None, :] * glow[..., None]
    alpha = np.maximum(alpha, _clip(glow * 0.9))

    # ---- the sphere ----
    inside = r <= 1.0
    z = np.sqrt(np.clip(1.0 - r2, 0.0, 1.0))          # sphere normal z
    # light from upper-left
    lx, ly, lz = -0.5, -0.55, 0.67
    lam = _clip(dx * lx + dy * ly + z * lz)
    limb = _clip(z)                                    # dark toward the edge
    # body colour: deep blue-teal core → brighter cyan mid → white hot spot
    core = np.array([0.06, 0.32, 0.62])
    mid = np.array([0.20, 0.72, 0.86])
    hot = np.array([0.85, 0.99, 1.00])
    tcol = _clip(0.35 + 0.65 * lam)
    body = (core[None, None, :] * (1 - tcol[..., None])
            + mid[None, None, :] * tcol[..., None])
    body += hot[None, None, :] * (lam ** 3.2)[..., None] * 0.6      # specular-ish core
    body *= (0.35 + 0.65 * limb)[..., None]                          # limb darkening
    # a bright fresnel rim
    rim = _clip((r - 0.82) / 0.18) * inside
    body += np.array([0.6, 0.95, 1.0])[None, None, :] * (rim ** 2)[..., None] * 0.8

    rgb = np.where(inside[..., None], body, rgb)
    alpha = np.where(inside, 1.0, alpha)

    # ---- one faint orbital ring (thin ellipse) hugging the sphere ----
    ring_r = 1.14
    ell = np.sqrt(dx * dx + (dy / 0.30) ** 2)          # squashed → an ellipse seen edge-on
    ring = np.exp(-((ell - ring_r) ** 2) / (2 * 0.018 ** 2)) * (r > 0.98)
    rgb += np.array([0.4, 0.9, 1.0])[None, None, :] * ring[..., None] * 0.8
    alpha = np.maximum(alpha, _clip(ring * 0.85))

    # Reinhard fold so nothing blows out, then to 8-bit
    rgb = rgb / (1.0 + rgb)
    rgb = _clip(rgb * 1.35)
    out = np.dstack([rgb, _clip(alpha)])
    return Image.fromarray((out * 255).astype(np.uint8), "RGBA")


def main() -> None:
    big = render(SS)
    sizes = [256, 128, 64, 48, 32, 16]
    frames = [big.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[0].save(OUT, format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[1:])
    # a PNG alongside, handy for the web favicon / docs
    big.resize((256, 256), Image.LANCZOS).save(OUT.with_suffix(".png"))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes) and orb.png")


if __name__ == "__main__":
    main()
