"""The HUD theme — a dark cyan/amber palette. One place owns the look."""
from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

# Palette
BG = "#080b0f"
PANEL = "#0d141b"
PANEL_HI = "#121b24"
CYAN = "#3fe0e0"
CYAN_DIM = "#1d6b6b"
AMBER = "#f5a623"
TEXT = "#e2edf1"
MUTED = "#7a8a93"
LINE = "#1b2730"

_STYLESHEET = f"""
* {{
    color: {TEXT};
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
    font-size: 14px;
}}
QWidget#Console, QWidget#Panel, QStackedWidget {{ background: {BG}; }}
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
    app.setStyleSheet(_STYLESHEET)
