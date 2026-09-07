"""Playback sense — re-export shim: the implementation lives in helix/adapters/mediasense.py
so the web voice loop (which must not import ui) shares the exact same meter."""
from helix.adapters.mediasense import *  # noqa: F401,F403
from helix.adapters.mediasense import MediaSense  # noqa: F401 — the one name callers use
