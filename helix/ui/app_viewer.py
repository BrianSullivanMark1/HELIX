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
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from helix.ui.chat_input import ChatInput
from helix.ui.theme import CYAN, LINE, MUTED

# Injected into the OPEN app to enter "point at an element" mode. On the next click it computes a stable
# CSS selector + a short outerHTML snippet for the element and hands them back to Python via the page
# title (a simple JS→Qt channel that needs no QWebChannel wiring). The page's real behaviour is untouched
# — one capture, then the listener removes itself and the cursor resets.
_PICK_JS = r"""
(function(){
  if (window.__helixPicking) return;
  window.__helixPicking = true;
  var prev = document.title;
  document.body.style.cursor = 'crosshair';
  function sel(el){
    if (!el || el.nodeType !== 1) return '';
    if (el.id) return '#' + el.id;
    var parts = [];
    while (el && el.nodeType === 1 && parts.length < 5 && el.tagName !== 'BODY'){
      var p = el.tagName.toLowerCase();
      if (el.className && typeof el.className === 'string'){
        var c = el.className.trim().split(/\s+/).slice(0,2).join('.');
        if (c) p += '.' + c;
      }
      var parent = el.parentNode;
      if (parent){
        var sib = Array.prototype.filter.call(parent.children, function(x){return x.tagName===el.tagName;});
        if (sib.length > 1) p += ':nth-of-type(' + (sib.indexOf(el)+1) + ')';
      }
      parts.unshift(p);
      if (el.id){ parts[0] = '#' + el.id; break; }
      el = el.parentNode;
    }
    return parts.join(' > ');
  }
  function done(el){
    document.removeEventListener('click', onClick, true);
    window.__helixPicking = false;
    document.body.style.cursor = '';
    var snip = (el.outerHTML || '').replace(/\s+/g,' ').slice(0,240);
    document.title = 'HELIXPICK::' + sel(el) + '::' + snip;
    setTimeout(function(){ document.title = prev; }, 50);
  }
  function onClick(e){ e.preventDefault(); e.stopPropagation(); done(e.target); }
  document.addEventListener('click', onClick, true);
})();
"""


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
        # Everything lives on an inner "card" frame. Normally it's full-bleed and invisible; when the
        # main window floats a 3D model over the orb (set_floating), the OUTER margins inset the card
        # and only the CARD gets the opaque rounded style — so the margins around it stay genuinely
        # transparent and the orb glows through (styling the whole widget would paint the full rect).
        self._card = QFrame()
        self._card.setObjectName("ViewerCard")
        card = QVBoxLayout(self._card)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(0)
        root.addWidget(self._card)

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
        point_btn = QPushButton("🎯 Point")
        point_btn.setObjectName("Nav")
        point_btn.setToolTip("Point at something on the page, then describe the change to just that part")
        point_btn.clicked.connect(self._start_pick)
        reload_btn = QPushButton("⟳")
        reload_btn.setObjectName("Nav")
        reload_btn.clicked.connect(self._reload)
        browser = QPushButton("↗ Browser")
        browser.setObjectName("Nav")
        browser.clicked.connect(self.openExternallyRequested.emit)
        row.addWidget(back)
        row.addWidget(self._title)
        row.addStretch(1)
        row.addWidget(point_btn)
        row.addWidget(edit_btn)
        row.addWidget(reload_btn)
        row.addWidget(browser)
        card.addWidget(bar)
        # The element the user pointed at (CSS selector + snippet), untrusted DATA from the page. Folded
        # into the next edit so the coder changes only that element; cleared after one use.
        self._pick_sel = ""
        self._pick_snip = ""

        self._web = QWebEngineView()
        # Built models load Three.js from a CDN, so a local file:// page must be allowed to fetch remote
        # URLs — otherwise QtWebEngine blocks the import and the model never builds (only the HUD shows).
        s = self._web.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        # If a very heavy model kills the render/GPU process, show a helpful message instead of a blank
        # page (and let Reload retry) — rather than the silent "sandbox limit" failure.
        self._web.page().renderProcessTerminated.connect(self._on_render_crash)
        # JS→Qt bridge for "point at an element": the injected script hands the picked selector/snippet
        # back through the page title (no QWebChannel needed).
        self._web.page().titleChanged.connect(self._on_title_changed)
        card.addWidget(self._web, stretch=1)

        # Live "Edit with AI" bar — hidden until you press Edit. Typing a change and pressing Update
        # iterates THIS build in place (the main window resolves it by the open build's slug — no name
        # guessing), and the page reloads itself when the build finishes.
        self._edit_bar = QWidget()
        self._edit_bar.setStyleSheet(f"border-top:1px solid {LINE};")
        erow = QHBoxLayout(self._edit_bar)
        erow.setContentsMargins(16, 10, 16, 10)
        erow.setSpacing(8)
        self._edit_input = ChatInput("Describe a change — HELIX updates this live…")  # Enter sends · Shift+Enter = new line
        self._edit_input.submitted.connect(self._send_edit)
        self._edit_status = QLabel("")
        self._edit_status.setStyleSheet(f"color:{MUTED};")
        update_btn = QPushButton("✨ Update")
        update_btn.setObjectName("Primary")
        update_btn.clicked.connect(self._send_edit)
        erow.addWidget(self._edit_input, stretch=1)
        erow.addWidget(self._edit_status)
        erow.addWidget(update_btn)
        self._edit_bar.setVisible(False)
        card.addWidget(self._edit_bar)

    def set_floating(self, on: bool) -> None:
        """Present as a floating card inset from the window edges (a 3D model over the orb) or as the
        normal full-bleed page (apps). The card itself stays OPAQUE on purpose — a transparent
        QWebEngine background is unreliable across GPUs (see the ShaderOrb note in the main window)."""
        m = 56 if on else 0
        self.layout().setContentsMargins(m, max(0, m - 22), m, m)
        self._card.setStyleSheet(
            "QFrame#ViewerCard{background:#0a0e14;border:1px solid rgba(63,224,224,0.25);"
            "border-radius:14px;}" if on else ""
        )

    def _toggle_edit(self) -> None:
        show = not self._edit_bar.isVisible()
        self._edit_bar.setVisible(show)
        if show:
            self._edit_status.setText("")
            self._edit_input.setFocus()

    def _start_pick(self) -> None:
        """Enter point-at-an-element mode: the user clicks something on the page, and the next edit is
        scoped to just that element."""
        self._edit_bar.setVisible(True)
        self._edit_status.setText("Click the part of the page you want to change…")
        try:
            self._web.page().runJavaScript(_PICK_JS)
        except Exception:  # noqa: BLE001 — no page / WebEngine hiccup: just leave edit mode open
            self._edit_status.setText("Couldn't start pointing here — describe the change instead.")

    def _on_title_changed(self, title: str) -> None:
        if not title.startswith("HELIXPICK::"):
            return
        try:
            _, sel, snip = title.split("::", 2)
        except ValueError:
            return
        self._pick_sel = sel.strip()
        self._pick_snip = snip.strip()
        short = (self._pick_sel or "that element")[:60]
        self._edit_bar.setVisible(True)
        self._edit_status.setText(f"Editing: {short} — now describe the change.")
        self._edit_input.setFocus()

    def _send_edit(self) -> None:
        text = self._edit_input.text().strip()
        if not text:
            return
        self._edit_input.clear()
        self._edit_status.setText("Updating…")
        if self._pick_sel:
            # Fold the pointed-at element into the change so the coder scopes the edit to it. The
            # selector/snippet is untrusted page DATA; edit_app_prompt fences the whole change as data.
            change = (
                f"Change ONLY this one element on the page and leave everything else exactly as it is. "
                f"The element is at CSS selector `{self._pick_sel}` and currently looks like: "
                f"{self._pick_snip}. The change the user wants: {text}"
            )
            self._pick_sel = self._pick_snip = ""
        else:
            change = text
        self.editRequested.emit(change)

    def set_edit_status(self, msg: str) -> None:
        """The main window pushes build progress/finish here so the edit bar reflects the live update."""
        self._edit_status.setText(msg)

    def load(self, path: Path, title: str) -> None:
        self._title.setText(title)
        self._edit_status.setText("")
        self._pick_sel = self._pick_snip = ""
        self._web.setUrl(QUrl.fromLocalFile(str(path)))

    def load_url(self, url: str, title: str) -> None:
        """Show a local backend app (a main.py server) running at `url`, inside HELIX — no browser."""
        self._title.setText(title)
        self._edit_status.setText("")
        self._pick_sel = self._pick_snip = ""
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
            "This hologram was too heavy to display</div>"
            "<div style='font-size:14px;line-height:1.5;opacity:.85'>It likely exceeded this machine's "
            "graphics memory. Try ⟳ Reload, or set the hologram detail to “Balanced” in Settings and "
            "rebuild.</div>"
            "</div></body>"
        )

    def _reload(self) -> None:
        self._web.reload()

    def clear(self) -> None:
        """Leaving the viewer: blank the page so animation/audio stops and the GL surface is released."""
        self._web.setUrl(QUrl("about:blank"))
