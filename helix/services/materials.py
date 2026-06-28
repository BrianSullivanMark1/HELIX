"""Procedural PBR texture presets for the parametric baker — pure numpy + scipy + PIL, deterministic and
in-process (no network, no new heavy deps). Gives flat solid-color primitives real surface — baseColor +
tangent-space normal + roughness + baked ambient occlusion — which is the other half of the gap to neural
output. Maps are FFT-generated (so they tile seamlessly), cached per preset, and attached to a primitive
via triplanar UVs by the baker.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter
from trimesh.visual.material import PBRMaterial

_SIZE = 256  # texture resolution (square, tileable)

# preset -> surface parameters. a/b are the two base colors interpolated by the height field; rough is the
# mean roughness, rvar its variation; metal the metalness; nstr the normal-map strength; ao the AO depth.
_PRESETS: dict[str, dict] = {
    "bark":         {"a": (0.24, 0.15, 0.08), "b": (0.46, 0.32, 0.18), "rough": 0.92, "rvar": 0.10, "metal": 0.0, "nstr": 3.2, "ao": 0.55},
    "wood":         {"a": (0.45, 0.30, 0.15), "b": (0.62, 0.43, 0.25), "rough": 0.55, "rvar": 0.16, "metal": 0.0, "nstr": 1.4, "ao": 0.30},
    "leaf":         {"a": (0.11, 0.38, 0.12), "b": (0.22, 0.56, 0.18), "rough": 0.70, "rvar": 0.12, "metal": 0.0, "nstr": 1.1, "ao": 0.40},
    "grass":        {"a": (0.18, 0.42, 0.16), "b": (0.30, 0.55, 0.22), "rough": 0.85, "rvar": 0.10, "metal": 0.0, "nstr": 1.0, "ao": 0.35},
    "stone":        {"a": (0.33, 0.33, 0.35), "b": (0.52, 0.52, 0.55), "rough": 0.88, "rvar": 0.12, "metal": 0.0, "nstr": 2.6, "ao": 0.60},
    "concrete":     {"a": (0.55, 0.55, 0.55), "b": (0.69, 0.69, 0.68), "rough": 0.90, "rvar": 0.08, "metal": 0.0, "nstr": 1.2, "ao": 0.40},
    "metal":        {"a": (0.60, 0.62, 0.66), "b": (0.80, 0.82, 0.87), "rough": 0.33, "rvar": 0.12, "metal": 1.0, "nstr": 0.6, "ao": 0.20},
    "rusted_metal": {"a": (0.30, 0.18, 0.10), "b": (0.56, 0.40, 0.30), "rough": 0.80, "rvar": 0.22, "metal": 0.6, "nstr": 2.0, "ao": 0.50},
    "panel":        {"a": (0.30, 0.32, 0.36), "b": (0.42, 0.44, 0.49), "rough": 0.50, "rvar": 0.10, "metal": 0.7, "nstr": 1.0, "ao": 0.30},
    "plastic":      {"a": (0.70, 0.72, 0.78), "b": (0.82, 0.84, 0.90), "rough": 0.40, "rvar": 0.06, "metal": 0.0, "nstr": 0.5, "ao": 0.20},
}

PRESETS = frozenset(_PRESETS)


def _fbm(seed: int, octaves: int = 5) -> np.ndarray:
    """Fractal value noise in [0,1] built by summing FFT-filtered octaves — periodic, so it tiles."""
    rng = np.random.default_rng(seed)
    fy = np.fft.fftfreq(_SIZE)[:, None]
    fx = np.fft.fftfreq(_SIZE)[None, :]
    r = np.sqrt(fx * fx + fy * fy)
    out = np.zeros((_SIZE, _SIZE))
    amp, total = 1.0, 0.0
    for o in range(octaves):
        cutoff = 0.02 * (2 ** o)  # bigger blobs first, finer detail each octave
        filt = np.exp(-((r / cutoff) ** 2))
        layer = np.real(np.fft.ifft2(np.fft.fft2(rng.standard_normal((_SIZE, _SIZE))) * filt))
        layer = (layer - layer.mean()) / (layer.std() + 1e-6)
        out += amp * layer
        total += amp
        amp *= 0.5
    out /= total
    return (out - out.min()) / (np.ptp(out) + 1e-6)


def _normal_from_height(h: np.ndarray, strength: float) -> np.ndarray:
    gy, gx = np.gradient(h * float(strength))
    n = np.stack([-gx, -gy, np.ones_like(h)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True) + 1e-9
    return ((n * 0.5 + 0.5) * 255).astype(np.uint8)


@lru_cache(maxsize=None)
def _texture_set(preset: str) -> tuple[Image.Image, Image.Image, Image.Image, Image.Image]:
    """(baseColor, metallicRoughness, normal, occlusion) PIL images for a preset. Cached per preset."""
    p = _PRESETS[preset]
    h = _fbm(seed=(zlib_crc(preset)))
    a, b = np.array(p["a"]), np.array(p["b"])
    base = a[None, None, :] + (b - a)[None, None, :] * h[..., None]
    base_img = Image.fromarray((np.clip(base, 0, 1) * 255).astype(np.uint8), "RGB")
    # glTF metallicRoughness: roughness in G, metalness in B.
    mr = np.zeros((_SIZE, _SIZE, 3))
    mr[..., 1] = np.clip(p["rough"] + p["rvar"] * (h - 0.5) * 2.0, 0.0, 1.0)
    mr[..., 2] = p["metal"]
    mr_img = Image.fromarray((mr * 255).astype(np.uint8), "RGB")
    normal_img = Image.fromarray(_normal_from_height(h, p["nstr"]), "RGB")
    soft = gaussian_filter(h, 3.0, mode="wrap")  # baked AO: valleys occluded, ridges bright
    ao = np.clip(1.0 - p["ao"] * (1.0 - soft), 0.0, 1.0)
    ao_img = Image.fromarray((np.stack([ao] * 3, axis=-1) * 255).astype(np.uint8), "RGB")
    return base_img, mr_img, normal_img, ao_img


def zlib_crc(text: str) -> int:
    import zlib

    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF


def material_for(preset: str, opacity: float = 1.0) -> PBRMaterial:
    """A textured PBR material for a preset (or None-safe flat fallback if the preset is unknown)."""
    base, mr, normal, ao = _texture_set(preset)
    kw = dict(
        name=preset,
        baseColorFactor=[1.0, 1.0, 1.0, max(0.0, min(1.0, opacity))],
        baseColorTexture=base,
        metallicRoughnessTexture=mr,
        normalTexture=normal,
        occlusionTexture=ao,
        metallicFactor=1.0,
        roughnessFactor=1.0,
    )
    if opacity < 1.0:
        kw["alphaMode"] = "BLEND"
    return PBRMaterial(**kw)
