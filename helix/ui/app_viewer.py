"""AppViewer — renders a built HTML app or 3D model INSIDE HELIX, no external browser tabs.

Part of the immutable shell. One reused QWebEngineView lives in the main window; opening another app
just loads it here, so tabs never pile up. Importing this module requires PyQt6-WebEngine — the main
window imports it defensively and falls back to the system browser if it isn't installed.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from helix.ui.theme import CYAN, LINE


class AppViewer(QWidget):
    """A header (Back / title / Reload / open-in-Browser) above a web view that runs the built page."""

    closeRequested = pyqtSignal()
    openExternallyRequested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Panel")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget()
        bar.setStyleSheet(f"border-bottom:1px solid {LINE};")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(8)
        back = QPushButton("←  Back")
        back.setObjectName("Nav")
        back.clicked.connect(self.closeRequested.emit)
        self._title = QLabel("")
        self._title.setStyleSheet(f"color:{CYAN};font-weight:600;")
        reload_btn = QPushButton("⟳")
        reload_btn.setObjectName("Nav")
        reload_btn.clicked.connect(self._reload)
        browser = QPushButton("↗ Browser")
        browser.setObjectName("Nav")
        browser.clicked.connect(self.openExternallyRequested.emit)
        row.addWidget(back)
        row.addWidget(self._title)
        row.addStretch(1)
        row.addWidget(reload_btn)
        row.addWidget(browser)
        root.addWidget(bar)

        self._web = QWebEngineView()
        # Built models load Three.js from a CDN, so a local file:// page must be allowed to fetch remote
        # URLs — otherwise QtWebEngine blocks the import and the model never builds (only the HUD shows).
        s = self._web.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        # If a very heavy model kills the render/GPU process, show a helpful message instead of a blank
        # page (and let Reload retry) — rather than the silent "sandbox limit" failure.
        self._web.page().renderProcessTerminated.connect(self._on_render_crash)
        root.addWidget(self._web, stretch=1)

    def load(self, path: Path, title: str) -> None:
        self._title.setText(title)
        self._web.setUrl(QUrl.fromLocalFile(str(path)))

    def _on_render_crash(self, *_args) -> None:
        """The web render process died (most often a too-heavy model exhausting the GPU). Show a clear
        message + path forward instead of a blank view."""
        self._web.setHtml(
            "<body style='margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
            "background:#080b0f;color:#9fc7c8;font-family:-apple-system,Segoe UI,sans-serif;text-align:center'>"
            "<div style='max-width:460px;padding:28px'>"
            "<div style='color:#3fe0e0;font-size:16px;font-weight:600;margin-bottom:8px'>"
            "This model was too heavy to display</div>"
            "<div style='font-size:14px;line-height:1.5;opacity:.85'>It likely exceeded this machine's "
            "graphics memory. Try ⟳ Reload, or set 3D model detail to “Balanced” in Settings and rebuild.</div>"
            "</div></body>"
        )

    def _reload(self) -> None:
        self._web.reload()

    def clear(self) -> None:
        """Leaving the viewer: blank the page so animation/audio stops and the GL surface is released."""
        self._web.setUrl(QUrl("about:blank"))
