"""HELIX launcher.

Thin entry point. (Voice STT pre-warming, which must happen before PyQt6 imports on Windows, will hook
in here when the speech adapter lands in phase 8.)
"""
from __future__ import annotations

import sys

from helix.app.cli import main

if __name__ == "__main__":
    sys.exit(main())
