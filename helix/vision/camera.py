"""Capture a frame from a camera (USB index or network stream) + a named-camera registry.

HELIX's "eyes" around the house are registered by name -> source in settings: a source is a USB index
(e.g. "0") or a stream URL (RTSP/HTTP — a phone IP-cam app, or a network camera by the fridge/laundry).
OpenCV handles both. Optional dependency, lazy-imported and guarded (mirrors `ai/speech.py`)."""
from __future__ import annotations

import os
from typing import Any

DEFAULT_CAMERA_INDEX = int(os.environ.get("HELIX_CAMERA_INDEX", "0") or "0")
CAMERAS_SETTING = "vision_cameras"  # [{"name": "fridge", "source": "rtsp://..."}, ...]


class CameraError(RuntimeError):
    """Raised when a camera frame can't be captured."""


def is_available() -> bool:
    """True if OpenCV is importable (does NOT open a camera)."""
    try:
        import cv2  # noqa: F401
    except Exception:
        return False
    return True


def resolve_source(source: Any):
    """Registry source -> an OpenCV source: an int for a USB index, else the stream URL string."""
    if isinstance(source, int):
        return source
    text = str(source).strip()
    return int(text) if text.isdigit() else text


def capture_jpeg(source: Any = DEFAULT_CAMERA_INDEX, *, warmup: int = 3, quality: int = 85) -> bytes:
    """Grab one frame from `source` (USB index or stream URL) and return JPEG bytes."""
    try:
        import cv2
    except Exception as error:
        raise CameraError("Camera needs opencv-python. Install it with:  pip install opencv-python") from error
    src = resolve_source(source)
    cap = None
    try:
        if isinstance(src, int) and hasattr(cv2, "CAP_DSHOW"):
            cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)  # reliable USB backend on Windows
        else:
            cap = cv2.VideoCapture(src)                 # network stream (RTSP/HTTP) or fallback
        if cap is None or not cap.isOpened():
            raise CameraError(f"Could not open camera source '{source}'.")
        frame, ok = None, False
        for _ in range(max(1, warmup)):  # discard warmup frames so exposure settles (and the stream buffers)
            ok, frame = cap.read()
        if not ok or frame is None:
            raise CameraError(f"Camera '{source}' opened but returned no frame.")
        encoded, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not encoded:
            raise CameraError("Failed to encode the camera frame.")
        return bytes(buffer)
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


# --- named-camera registry (the "eyes" around the house) --------------------- #

def list_cameras(settings: Any) -> list[dict]:
    cams = settings.get(CAMERAS_SETTING) or []
    return [c for c in cams if isinstance(c, dict) and c.get("name")]


def get_camera(settings: Any, name: str) -> dict | None:
    want = (name or "").strip().lower()
    for cam in list_cameras(settings):
        if str(cam.get("name", "")).strip().lower() == want:
            return cam
    return None


def add_camera(settings: Any, name: str, source: Any) -> dict:
    """Register (or update) a named camera. `source` is a USB index or a stream URL."""
    name = (name or "").strip()
    if not name:
        raise CameraError("A camera needs a name.")
    cams = [c for c in list_cameras(settings) if str(c.get("name", "")).strip().lower() != name.lower()]
    rec = {"name": name, "source": str(source).strip()}
    cams.append(rec)
    settings.set(CAMERAS_SETTING, cams)
    return rec


def remove_camera(settings: Any, name: str) -> bool:
    before = list_cameras(settings)
    after = [c for c in before if str(c.get("name", "")).strip().lower() != (name or "").strip().lower()]
    settings.set(CAMERAS_SETTING, after)
    return len(after) < len(before)


def capture_named(settings: Any, name: str, **kwargs) -> bytes:
    """Capture from a registered camera by name."""
    cam = get_camera(settings, name)
    if cam is None:
        raise CameraError(f"No camera named '{name}'. Add one first.")
    return capture_jpeg(cam.get("source", DEFAULT_CAMERA_INDEX), **kwargs)
