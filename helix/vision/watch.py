"""Local motion detection for HELIX's eyes (§vision) — cheap, no API cost.

Frame-difference motion detection (OpenCV), so an always-on door/area watch spends a Claude vision call
only when something actually moves. Stateful per camera; optional/guarded like the rest of vision. The
Console runs this on a slow timer for a camera named "door" (or any watch camera) and, on motion,
captures one frame, asks who/what it is, and announces it — proactively, only when HELIX is idle.
"""
from __future__ import annotations


class MotionDetector:
    """Detects motion between successive JPEG frames by grayscale frame-differencing.

    `threshold` is the fraction of pixels that must change; `pixel_delta` is the per-pixel intensity
    change that counts as 'changed'. Tune up if a noisy camera false-triggers."""

    def __init__(self, threshold: float = 0.014, pixel_delta: int = 25) -> None:
        self._prev = None
        self._threshold = threshold
        self._pixel_delta = pixel_delta

    def reset(self) -> None:
        self._prev = None

    def check(self, jpeg: bytes) -> bool:
        """True if this frame differs enough from the previous one. First frame is always False."""
        try:
            import cv2
            import numpy as np
        except Exception:
            return False
        if not jpeg:
            return False
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if frame is None:
            return False
        frame = cv2.GaussianBlur(cv2.resize(frame, (160, 120)), (5, 5), 0)
        prev = self._prev
        self._prev = frame
        if prev is None:
            return False
        diff = cv2.absdiff(prev, frame)
        changed = int((diff > self._pixel_delta).sum())
        return (changed / float(frame.size)) >= self._threshold
