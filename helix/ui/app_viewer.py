"""AppViewer — renders a built HTML app or 3D model INSIDE HELIX, no external browser tabs.

Part of the immutable shell. One reused QWebEngineView lives in the main window; opening another app
just loads it here, so tabs never pile up. Importing this module requires PyQt6-WebEngine — the main
window imports it defensively and falls back to the system browser if it isn't installed.
"""
from __future__ import annotations

from html import escape
from pathlib import Path

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from helix.ui.theme import CYAN, LINE, MUTED


class AppViewer(QWidget):
    """A header (Back / title / Edit / Reload / open-in-Browser) above a web view that runs the built
    page, with a live 'Edit with AI' bar along the bottom that iterates THIS build in place."""

    closeRequested = pyqtSignal()
    openExternallyRequested = pyqtSignal()
    editRequested = pyqtSignal(str)  # the live edit bar — a plain-language change to the open build

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
        edit_btn = QPushButton("✨ Edit")
        edit_btn.setObjectName("Nav")
        edit_btn.setToolTip("Describe a change and HELIX updates this build live")
        edit_btn.clicked.connect(self._toggle_edit)
        reload_btn = QPushButton("⟳")
        reload_btn.setObjectName("Nav")
        reload_btn.clicked.connect(self._reload)
        browser = QPushButton("↗ Browser")
        browser.setObjectName("Nav")
        browser.clicked.connect(self.openExternallyRequested.emit)
        row.addWidget(back)
        row.addWidget(self._title)
        row.addStretch(1)
        row.addWidget(edit_btn)
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

        # Live "Edit with AI" bar — hidden until you press Edit. Typing a change and pressing Update
        # iterates THIS build in place (the main window resolves it by the open build's slug — no name
        # guessing), and the page reloads itself when the build finishes.
        self._edit_bar = QWidget()
        self._edit_bar.setStyleSheet(f"border-top:1px solid {LINE};")
        erow = QHBoxLayout(self._edit_bar)
        erow.setContentsMargins(16, 10, 16, 10)
        erow.setSpacing(8)
        self._edit_input = QLineEdit()
        self._edit_input.setPlaceholderText("Describe a change — HELIX updates this live…")
        self._edit_input.returnPressed.connect(self._send_edit)
        self._edit_status = QLabel("")
        self._edit_status.setStyleSheet(f"color:{MUTED};")
        update_btn = QPushButton("✨ Update")
        update_btn.setObjectName("Primary")
        update_btn.clicked.connect(self._send_edit)
        erow.addWidget(self._edit_input, stretch=1)
        erow.addWidget(self._edit_status)
        erow.addWidget(update_btn)
        self._edit_bar.setVisible(False)
        root.addWidget(self._edit_bar)

    def _toggle_edit(self) -> None:
        show = not self._edit_bar.isVisible()
        self._edit_bar.setVisible(show)
        if show:
            self._edit_status.setText("")
            self._edit_input.setFocus()

    def _send_edit(self) -> None:
        text = self._edit_input.text().strip()
        if not text:
            return
        self._edit_input.clear()
        self._edit_status.setText("Updating…")
        self.editRequested.emit(text)

    def set_edit_status(self, msg: str) -> None:
        """The main window pushes build progress/finish here so the edit bar reflects the live update."""
        self._edit_status.setText(msg)

    def load(self, path: Path, title: str) -> None:
        self._title.setText(title)
        self._edit_status.setText("")
        self._web.setUrl(QUrl.fromLocalFile(str(path)))

    def load_url(self, url: str, title: str) -> None:
        """Show a local backend app (a main.py server) running at `url`, inside HELIX — no browser."""
        self._title.setText(title)
        self._edit_status.setText("")
        self._web.setUrl(QUrl(url))

    def show_starting(self, title: str) -> None:
        """A brief 'starting…' page while the build's local server boots, so the viewer isn't blank."""
        self._title.setText(title)
        safe = escape(title)
        self._web.setHtml(
            "<body style='margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
            "background:#080b0f;color:#9fc7c8;font-family:-apple-system,Segoe UI,sans-serif'>"
            f"<div style='opacity:.8'>Starting {safe}…</div></body>"
        )

    def show_notice(self, title: str, body: str) -> None:
        """A centered message in the viewer (e.g. a build's server failed to start)."""
        self._title.setText(title)
        self._web.setHtml(
            "<body style='margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
            "background:#080b0f;color:#9fc7c8;font-family:-apple-system,Segoe UI,sans-serif;text-align:center'>"
            f"<div style='max-width:440px;padding:26px'>"
            f"<div style='color:#3fe0e0;font-size:16px;font-weight:600;margin-bottom:8px'>{escape(title)}</div>"
            f"<div style='font-size:14px;line-height:1.5;opacity:.85'>{escape(body)}</div></div></body>"
        )

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
