"""HELIX's eyes (§vision) — connect a camera to the conversation.

Capture a frame (`camera`) and have Claude vision describe it (`analyze`): identify a tool and explain
how to use it, describe a person at the door for awareness, or answer a free question about what's in
view. Surfaced through the Xpert assistant's `look` tool, so you just ask and HELIX looks.

OpenCV is an optional dependency, lazy-imported and guarded (like the speech/STT layers) so the rest of
HELIX runs without it. This is the first of X's embodied faculties; hardware I/O (`helix/devices/`) and
deeper person/online profiling come later (the latter carries real privacy/legal weight — gated).
"""
from __future__ import annotations
