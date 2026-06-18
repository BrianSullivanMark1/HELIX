"""Capture a single frame from a camera as JPEG bytes (OpenCV).

Optional dependency, lazy-imported and guarded (mirrors `ai/speech.py` and `ai/transcribe.py`): if
OpenCV or a camera is missing, `is_available()` is False and callers degrade gracefully. On Windows the
DirectShow backend (`CAP_DSHOW`) is the reliable one. Verified to coexist with Qt's QApplication (no
native conflict like the ctranslate2 case)."""
from __future__ import annotations

import os

DEFAULT_CAMERA_INDEX = int(os.environ.get("HELIX_CAMERA_INDEX", "0") or "0")


class CameraError(RuntimeError):
    """Raised when a camera frame can't be captured."""


def is_available() -> bool:
    """True if OpenCV is importable (does NOT open a camera)."""
    try:
        import cv2  # noqa: F401
    except Exception:
        return False
    return True


def capture_jpeg(index: int = DEFAULT_CAMERA_INDEX, *, warmup: int = 3, quality: int = 85) -> bytes:
    """Grab one frame from camera `index` and return JPEG bytes. Raises CameraError on any failure.

    A few frames are read and discarded first so auto-exposure/white-balance settle before the keeper."""
    try:
        import cv2
    except Exception as error:
        raise CameraError("Camera needs opencv-python. Install it with:  pip install opencv-python") from error
    cap = None
    try:
        backend = getattr(cv2, "CAP_DSHOW", None)
        cap = cv2.VideoCapture(index, backend) if backend is not None else cv2.VideoCapture(index)
        if cap is None or not cap.isOpened():
            raise CameraError(f"Could not open camera {index}.")
        frame, ok = None, False
        for _ in range(max(1, warmup)):
            ok, frame = cap.read()
        if not ok or frame is None:
            raise CameraError("Camera opened but returned no frame.")
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
