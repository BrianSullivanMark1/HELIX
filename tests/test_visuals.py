"""Inline viz widgets paint headlessly without error — every chart kind, plus odd/empty data.

The chart code is QPainter, so it's verified on Qt's 'offscreen' platform (no display needed). Good-data
cases call each painter DIRECTLY so a real bug raises (rather than being hidden by paintEvent's
fall-back-to-bars guard); empty/malformed cases go through the full paintEvent via grab().
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.ui.console_view import ConsoleView, _ChartWidget  # noqa: E402
from helix.ui.orb import OrbState, PresenceOrb  # noqa: E402
from helix.ui.voice import _pcm_bands  # noqa: E402


@pytest.fixture(scope="module")
def _app():
    app = QApplication.instance() or QApplication([])
    yield app


_DATA = [{"label": f"Q{i}", "value": v} for i, v in enumerate([10, 25, 7, 18, 31], 1)]


@pytest.mark.parametrize("kind", ["bar", "column", "line", "area", "pie", "donut"])
def test_each_painter_runs_without_error(_app, kind):
    w = _ChartWidget({"type": "chart", "title": "Quarterly", "unit": "$", "kind": kind, "data": _DATA})
    w.resize(460, 260)
    w._t = 1.0
    pm = QPixmap(460, 260)
    p = QPainter(pm)
    area = QRectF(0.0, 28.0, 460.0, 232.0)
    try:
        if kind in ("pie", "donut"):
            w._paint_pie(p, area, donut=kind == "donut")
        elif kind in ("line", "area"):
            w._paint_line(p, area, fill=kind == "area")
        else:
            w._paint_bars(p, area)
    finally:
        p.end()


@pytest.mark.parametrize("kind", ["bar", "line", "area", "pie", "donut", "unknown-kind", None])
@pytest.mark.parametrize("data", [_DATA, [], [{"label": "a"}], [{"value": "x"}], "nonsense", None])
def test_full_paintevent_survives_any_data(_app, kind, data):
    spec = {"type": "chart", "data": data}
    if kind is not None:
        spec["kind"] = kind
    w = _ChartWidget(spec)
    w.resize(440, 240)
    w.render_now()
    assert not w.grab().isNull()  # grab() forces the real paintEvent (with its fall-back guard)


def test_table_numeric_cells_are_detected():
    # Right-alignment of value columns keys off this helper.
    assert ConsoleView._looks_numeric("1,234")
    assert ConsoleView._looks_numeric("$42")
    assert ConsoleView._looks_numeric("87%")
    assert not ConsoleView._looks_numeric("Revenue")
    assert not ConsoleView._looks_numeric("")


@pytest.mark.parametrize("state", list(OrbState))
def test_orb_paints_in_every_state_with_a_spectrum(_app, state):
    orb = PresenceOrb()
    orb.resize(320, 320)
    orb.set_state(state)
    orb.set_level(0.7)
    orb.set_bands([0.2, 0.9, 0.4, 0.7, 0.1] * 4)  # 20 values → truncated to 16
    orb._tick()  # advance the eased values once so the spectral ring has magnitude
    assert not orb.grab().isNull()


def test_orb_set_bands_is_robust_to_odd_input(_app):
    orb = PresenceOrb()
    orb.resize(200, 200)
    for bands in ([], [0.5], [1.0] * 64, "nonsense", None):
        orb.set_bands(bands)  # defensive: never raises, even on garbage
        orb._tick()
        assert not orb.grab().isNull()


def test_pcm_bands_silence_is_near_zero():
    pytest.importorskip("numpy")
    bands = _pcm_bands(b"\x00\x00" * 1024)
    assert len(bands) == 16 and max(bands) < 0.05


def test_pcm_bands_loud_tone_lights_up():
    pytest.importorskip("numpy")
    import array
    import math as _math

    n = 1024
    tone = array.array("h", [int(22000 * _math.sin(2 * _math.pi * 8 * i / n)) for i in range(n)])
    bands = _pcm_bands(tone.tobytes())
    assert len(bands) == 16 and max(bands) > 0.1


def test_transcript_fade_in_applies_an_effect_without_crashing(_app):
    from PyQt6.QtWidgets import QLabel

    lbl = QLabel("hello")
    ConsoleView._animate_in(lbl)  # staticmethod: applies an opacity effect + fade animation
    assert lbl.graphicsEffect() is not None


def test_bundled_display_font_loads(_app):
    from helix.ui.theme import load_display_font

    assert load_display_font() == "Orbitron"  # the bundled sci-fi face is present and Qt-loadable


def test_apply_theme_runs_with_the_display_font(_app):
    from helix.ui.theme import apply_theme

    apply_theme(_app)  # sets palette + stylesheet (incl. the display-font rules); must not raise
    assert "Orbitron" in _app.styleSheet()


def test_shader_orb_exposes_the_orb_interface():
    from helix.ui.shader_orb import ShaderOrb

    for m in ("set_state", "set_level", "set_bands"):
        assert callable(getattr(ShaderOrb, m))
    assert hasattr(ShaderOrb, "clicked")


def test_shader_orb_falls_back_to_the_painter_orb_without_webengine(_app, monkeypatch):
    # Force the no-WebEngine path: ShaderOrb must construct, drive, and paint via the QPainter fallback.
    import helix.ui.shader_orb as so

    monkeypatch.setattr(so, "_HAVE_WEBENGINE", False)
    orb = so.ShaderOrb()
    orb.resize(300, 300)
    orb.set_state(OrbState.SPEAKING)
    orb.set_level(0.6)
    orb.set_bands([0.5] * 16)  # forwarded to the fallback; the (absent) WebGL layer is a no-op
    assert orb._view is None and not orb.grab().isNull()


# ---- AppViewer: a hologram's export links actually save a file in-app --------------------------------
#
# The viewer is constructed against a FAKE QWebEngineView (a plain QWidget with a recording page/profile):
# QtWebEngine cannot spin up its render process under the offscreen test platform, and everything these
# pins care about — that construction wires the profile's downloadRequested, and what the handler does
# with a request — lives entirely on the Python side of that seam.


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for fn in list(self.slots):
            fn(*a)


class _FakeProfile:
    def __init__(self):
        self.downloadRequested = _Sig()


class _FakePage:
    def __init__(self):
        self.renderProcessTerminated = _Sig()
        self.titleChanged = _Sig()
        self._profile = _FakeProfile()

    def profile(self):
        return self._profile

    def runJavaScript(self, *_a):
        pass


class _FakeSettings:
    def setAttribute(self, *_a):
        pass


def _fake_view_class():
    from PyQt6.QtWidgets import QWidget

    class _FakeView(QWidget):
        def __init__(self):
            super().__init__()
            self._page = _FakePage()

        def settings(self):
            return _FakeSettings()

        def page(self):
            return self._page

        def setUrl(self, *_a):
            pass

        def setHtml(self, *_a):
            pass

    return _FakeView


class _FakeDownload:
    """Just enough of QWebEngineDownloadRequest: the page suggests a name, the handler picks the directory,
    accept() starts it, isFinishedChanged fires once with the final state."""

    def __init__(self, name="model.stl"):
        from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest as R

        self._R = R
        self._name = name
        self.directory = None
        self.accepted = False
        self._state = R.DownloadState.DownloadRequested
        self.isFinishedChanged = _Sig()

    def downloadFileName(self):
        return self._name

    def setDownloadDirectory(self, d):
        self.directory = d

    def accept(self):
        self.accepted = True
        self._state = self._R.DownloadState.DownloadInProgress

    def state(self):
        return self._state

    def interruptReasonString(self):
        return "the disk is full"

    def finish(self, ok=True):
        st = self._R.DownloadState
        self._state = st.DownloadCompleted if ok else st.DownloadInterrupted
        self.isFinishedChanged.emit()


def _viewer(monkeypatch):
    # PyQt refuses to import QtWebEngineWidgets after a QApplication exists unless this attribute is set; it
    # is only a flag here (no real web view is ever created), so set it rather than skip when another test
    # module created the app first.
    from PyQt6.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    pytest.importorskip("PyQt6.QtWebEngineWidgets")
    import helix.ui.app_viewer as av

    monkeypatch.setattr(av, "QWebEngineView", _fake_view_class())
    return av.AppViewer()


def test_app_viewer_accepts_page_downloads_into_the_downloads_folder(_app, monkeypatch):
    # Without a slot on downloadRequested, QtWebEngine cancels an unaccepted download silently — the four
    # export buttons of a hologram (STL / 3MF / SCAD / preview) would do NOTHING in-app. Construction must
    # wire exactly one handler, and that handler must point the request at Downloads (keeping the page's
    # suggested name so Qt can uniquify it), accept it, and say so in the header when it lands.
    from PyQt6.QtCore import QStandardPaths

    v = _viewer(monkeypatch)
    slots = v._web.page().profile().downloadRequested.slots
    assert len(slots) == 1
    req = _FakeDownload("model.stl")
    slots[0](req)
    assert req.accepted
    assert req.directory == QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    assert req.directory  # a real folder, not "" (which Qt would read as "nowhere")
    assert v._status.text() == "Saving model.stl…"
    req.finish(ok=True)
    assert v._status.text() == "Saved model.stl to Downloads"


def test_app_viewer_says_when_a_download_did_not_land(_app, monkeypatch):
    # An interrupted export must not leave "Saving…" on screen forever: the header says it failed and why.
    v = _viewer(monkeypatch)
    req = _FakeDownload("model.3mf")
    v._web.page().profile().downloadRequested.emit(req)
    req.finish(ok=False)
    assert v._status.text() == "Couldn't save model.3mf — the disk is full"


def test_app_viewer_header_toast_clears_itself_but_never_a_newer_line(_app, monkeypatch):
    # _say() clears its own line after a hold; a line that was replaced in the meantime is left alone, so
    # "Saved model.stl" can never be wiped by the timer of an older "Saving…" — driven by calling the
    # scheduled clear directly (no wall-clock wait).
    from PyQt6.QtCore import QTimer

    scheduled = []
    v = _viewer(monkeypatch)
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, fn: scheduled.append(fn)))
    v._say("first")
    v._say("second")
    assert len(scheduled) == 2
    scheduled[0]()  # the OLD timer fires: "second" is not its line, so it stays
    assert v._status.text() == "second"
    scheduled[1]()
    assert v._status.text() == ""
