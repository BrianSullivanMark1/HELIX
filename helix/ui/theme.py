"""The HUD theme — a dark cyan/amber palette. One place owns the look."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication

# Palette
BG = "#080b0f"
PANEL = "#0d141b"
PANEL_HI = "#121b24"
CYAN = "#3fe0e0"
CYAN_DIM = "#1d6b6b"
AMBER = "#f5a623"
GOLD = "#ffc857"  # the orb's warm "I'm speaking" tone
TEXT = "#e2edf1"
MUTED = "#7a8a93"
LINE = "#1b2730"

# Build-status palette — one shared meaning across the orb, the menu tiles, and the Console legend:
#   blue  = idle / done-and-seen (the default CYAN above)
#   yellow = working on it (a build is in progress)
#   green = finished successfully, not yet reopened
#   red   = it errored
STATUS_WORKING = "#ffcf45"  # yellow
STATUS_DONE = "#3fe07a"     # green
STATUS_ERROR = "#ff5d62"    # red

_STYLESHEET = f"""
* {{
    color: {TEXT};
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
    font-size: 14px;
}}
/* The orb is the window background; the Root paints the dark field behind it, everything else is
   transparent so the Presence shows through on every screen. */
QWidget#Root {{ background: {BG}; }}
QWidget#Console, QWidget#Panel, QWidget#Overlay, QStackedWidget {{ background: transparent; }}
QLabel#Title {{ font-size: 18px; font-weight: 600; letter-spacing: 1px; }}
QLabel#Status {{ color: {MUTED}; font-size: 13px; letter-spacing: 0.5px; }}
QLabel#Banner {{ color: {AMBER}; }}

QPushButton {{
    background: {PANEL_HI};
    border: 1px solid {LINE};
    border-radius: 10px;
    padding: 9px 16px;
}}
QPushButton:hover {{ border-color: {CYAN_DIM}; }}
QPushButton:pressed {{ background: {PANEL}; }}
QPushButton#Primary {{
    background: transparent;
    border: 1px solid {CYAN_DIM};
    color: {CYAN};
}}
QPushButton#Primary:hover {{ border-color: {CYAN}; }}
QPushButton#Nav {{ background: transparent; border: none; color: {MUTED}; padding: 6px 12px; }}
QPushButton#Nav:hover {{ color: {CYAN}; }}

QLineEdit, QTextEdit {{
    background: {PANEL};
    border: 1px solid {LINE};
    border-radius: 10px;
    padding: 10px 14px;
    selection-background-color: {CYAN_DIM};
}}
QLineEdit:focus, QTextEdit:focus {{ border-color: {CYAN_DIM}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

QFrame#Card {{
    background: {PANEL};
    border: 1px solid {LINE};
    border-radius: 14px;
}}
QFrame#Card:hover {{ border-color: {CYAN_DIM}; }}
"""


def load_display_font() -> str:
    """Load the bundled sci-fi display face (Orbitron). Returns its family, or '' if unavailable so
    callers fall back to the system UI font. The path is package-relative, so it resolves in dev AND in
    the PyInstaller-frozen build (where the assets are collected next to this module)."""
    path = Path(__file__).resolve().parent / "assets" / "fonts" / "Orbitron.ttf"
    try:
        fid = QFontDatabase.addApplicationFont(str(path))
        fams = QFontDatabase.applicationFontFamilies(fid)
        return fams[0] if fams else ""
    except Exception:
        return ""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.Base, QColor(PANEL))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(PANEL_HI))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(CYAN_DIM))
    app.setPalette(pal)
    # The HELIX wordmark + section titles wear the geometric display face (a quiet JARVIS signature);
    # everything else stays in the clean UI font. Falls back gracefully if the font didn't load.
    family = load_display_font()
    sheet = _STYLESHEET
    if family:
        sheet += (
            f'\nQLabel#Title {{ font-family: "{family}"; letter-spacing: 3px; }}\n'
            f'QLabel#Brand {{ font-family: "{family}"; letter-spacing: 3px; }}\n'
        )
    app.setStyleSheet(sheet)
