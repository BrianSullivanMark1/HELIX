"""SettingsView — the save round-trip, the slim review-only Connections manager, the statuses."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.services.connections import CONNECTABLE  # noqa: E402
from helix.ui import settings_view as sv  # noqa: E402
from helix.ui.settings_view import SettingsView  # noqa: E402


class _Settings:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class _Conns:
    def __init__(self):
        self.v = {}

    def value(self, env):
        return self.v.get(env, "")

    def set_value(self, env, val):
        self.v[env] = val


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


def test_settings_renders_and_saves(_app):
    s, c = _Settings(), _Conns()
    view = SettingsView(s, c)
    assert not view.grab().isNull()  # the redesigned form renders

    view._key.setText("sk-ant-abc")
    view._save()

    assert s.get("claude_api_key") == "sk-ant-abc"


def test_status_pills_reflect_set_state(_app):
    s, c = _Settings(), _Conns()
    s.d["claude_api_key"] = "sk-ant-present"
    view = SettingsView(s, c)  # reload() runs in __init__
    # the Claude key status shows Set; the unset subscription token shows Not set
    labels = [lbl.text() for lbl, _ in view._statuses]
    assert "● Set" in labels and "○ Not set" in labels


def test_connections_manager_shows_one_row_per_service(_app):
    view = SettingsView(_Settings(), _Conns())
    assert {sid for sid, _dot, _btn in view._conn_rows} == set(CONNECTABLE)
    # nothing connected → every dot is hollow and every Remove is hidden; no key field exists
    for _sid, dot, remove in view._conn_rows:
        assert dot.text() == "○"
        assert not remove.isVisibleTo(view)
    assert not hasattr(view, "_conn_fields")


def test_connected_service_shows_a_dot_and_remove_clears_it(_app):
    s, c = _Settings(), _Conns()
    c.set_value("SLACK_TOKEN", "xoxp-1")
    view = SettingsView(s, c)
    rows = {sid: (dot, btn) for sid, dot, btn in view._conn_rows}
    dot, remove = rows["slack"]
    assert dot.text() == "●" and remove.isVisibleTo(view)
    remove.click()
    assert c.value("SLACK_TOKEN") == ""  # cleared from the secrets store
    assert dot.text() == "○" and not remove.isVisibleTo(view)


def test_settings_store_service_connects_and_removes(_app, monkeypatch):
    monkeypatch.setitem(
        CONNECTABLE, "fakeco", ("FakeCo", "settings", (("fake_api_key", "FakeCo key", "fk-…"),))
    )
    s, c = _Settings(), _Conns()
    s.d["fake_api_key"] = "fk-123"
    view = SettingsView(s, c)
    rows = {sid: (dot, btn) for sid, dot, btn in view._conn_rows}
    dot, remove = rows["fakeco"]
    assert dot.text() == "●"
    remove.click()
    assert s.get("fake_api_key") == ""  # cleared from Settings, not the secrets store
    assert c.v == {}
    assert dot.text() == "○"


def test_two_credential_service_needs_both_to_show_connected(_app):
    s, c = _Settings(), _Conns()
    c.set_value("ALPACA_API_KEY", "AK-1")  # the secret is still missing
    view = SettingsView(s, c)
    dots = {sid: dot for sid, dot, _btn in view._conn_rows}
    assert dots["alpaca"].text() == "○"
    c.set_value("ALPACA_SECRET_KEY", "sec-2")
    view.reload()
    assert dots["alpaca"].text() == "●"


class _Sub:
    """Mirrors SubscriptionBrain.active(): live only when a token is saved AND the engine is present.

    Takes allow_probe like the real brain: the view must ask WITHOUT probing, because a real probe
    spawns claude.exe and this label is built before the first frame."""
    def __init__(self, settings, capable=True):
        self._settings = settings
        self._capable = capable  # SDK + Claude Code CLI reachable
        self.probed = []         # records what the view asked for, so the test can assert it didn't probe

    def active(self, *, allow_probe: bool = True):
        self.probed.append(allow_probe)
        token = (self._settings.get("claude_code_oauth_token") or "").strip()
        return self._capable and bool(token)


def test_brain_status_label_never_probes_for_a_cli(_app):
    """Pins the GUI-thread rule: resolving whether claude.exe will launch means spawning it, so the
    view must ask with allow_probe=False."""
    s = _Settings()
    s.d["claude_code_oauth_token"] = "sk-ant-oat01-tok"
    sub = _Sub(s, capable=True)
    view = SettingsView(s, _Conns(), subscription=sub)
    view._save()
    assert sub.probed, "the view never asked which brain is live"
    assert all(p is False for p in sub.probed), (
        f"SettingsView asked to probe on the GUI thread: {sub.probed}"
    )


def test_brain_status_shows_subscription_when_active(_app):
    s = _Settings()
    s.d["claude_code_oauth_token"] = "sk-ant-oat01-tok"
    view = SettingsView(s, _Conns(), subscription=_Sub(s, capable=True))
    assert "on your claude subscription" in view._brain_status.text().lower()


def test_brain_status_shows_api_meter_when_only_key(_app):
    s = _Settings()
    s.d["claude_api_key"] = "sk-ant-key"
    view = SettingsView(s, _Conns(), subscription=_Sub(s, capable=True))
    assert "on the api meter" in view._brain_status.text().lower()


def test_brain_status_flags_token_saved_but_cli_unreachable(_app):
    # token present, engine NOT reachable, no key → honest in-between state
    s = _Settings()
    s.d["claude_code_oauth_token"] = "sk-ant-oat01-tok"
    view = SettingsView(s, _Conns(), subscription=_Sub(s, capable=False))
    txt = view._brain_status.text().lower()
    assert "token saved" in txt and "reachable" in txt


def test_brain_status_not_connected_when_empty(_app):
    s = _Settings()
    view = SettingsView(s, _Conns(), subscription=_Sub(s, capable=True))
    assert "not connected" in view._brain_status.text().lower()


def test_brain_status_flips_to_subscription_after_saving_a_token(_app):
    s = _Settings()
    view = SettingsView(s, _Conns(), subscription=_Sub(s, capable=True))
    assert "not connected" in view._brain_status.text().lower()
    view._oauth_token.setText("sk-ant-oat01-fresh")
    view._save()
    assert s.get("claude_code_oauth_token") == "sk-ant-oat01-fresh"
    assert "on your claude subscription" in view._brain_status.text().lower()


class _FailingSub(_Sub):
    """A subscription brain whose STRUCTURE is flawless — token saved, SDK present, engine launchable
    — and whose last real request died anyway. Exactly the machine active() cannot tell apart from a
    healthy one, and the machine last_failure() was added for."""

    def __init__(self, settings, failure):
        super().__init__(settings, capable=True)
        self.failure = failure

    def last_failure(self):
        return self.failure


def test_a_rail_that_looks_healthy_but_failed_its_last_request_is_not_called_off_the_meter(_app):
    # The bill-shaped bug: token fine, engine fine, every request dying, HELIX quietly running each
    # one on the API key — while this screen, the one the user opens to diagnose it, said "off the
    # API meter" and sent them back to a token that was never wrong.
    s = _Settings()
    s.d["claude_code_oauth_token"] = "sk-ant-oat01-tok"
    s.d["claude_api_key"] = "sk-ant-key"
    sub = _FailingSub(s, "CreateProcess failed: the command line is too long")
    view = SettingsView(s, _Conns(), subscription=sub)
    txt = view._brain_status.text().lower()
    assert "off the api meter" not in txt
    assert "didn't go through" in txt
    tip = view._brain_status.toolTip()
    assert "command line is too long" in tip       # the real cause, on demand
    # …and which rail a fallback lands on. It has to be THIS phrase: both halves of the tooltip say
    # "api key" (the other one says there is none to fall back on), so that substring cannot tell the
    # two apart, and this fixture saved a key — the billed half is the one that must be rendered.
    assert "billed to the meter" in tip.lower()


@pytest.mark.parametrize("api_key", ["sk-ant-key", ""])
def test_the_failure_tooltip_never_claims_to_know_what_happened_next(_app, api_key):
    """A bare error string says a request DIED. It does not say what became of it, and this screen
    guesses nothing.

    conversation.py only re-runs a failed subscription request on the API key when no tool had been
    dispatched yet; once a build was enqueued or a reminder set, re-running would double those side
    effects, so it returns a soft partial and the API rail is never touched. So "HELIX used your API
    key for that one, so it was billed to the meter" could invent a charge that never happened — and
    "there was no API key to fall back on, so that one didn't get an answer" could deny an answer the
    user was actually given. Everything the tooltip says about what follows a failure must therefore
    be stated as the standing arrangement (conditional), not as this request's history."""
    s = _Settings()
    s.d["claude_code_oauth_token"] = "sk-ant-oat01-tok"
    if api_key:
        s.d["claude_api_key"] = api_key
    view = SettingsView(s, _Conns(), subscription=_FailingSub(s, "the engine went away mid-turn"))
    body = view._brain_status.toolTip().split("\n\n", 1)[1].lower()
    assert body.startswith("if a request can't finish"), (
        f"the tooltip states as fact what happened after the failure: {body!r}"
    )
    # The actionable half survives the softening: which rail money would land on, or that none exists.
    assert ("api key" in body) and ("billed to the meter" in body) == bool(api_key)


def test_the_off_the_meter_line_comes_back_once_the_rail_works_again(_app):
    # The recorded failure is cleared by the next success, so a single blip must not brand the panel
    # amber forever.
    s = _Settings()
    s.d["claude_code_oauth_token"] = "sk-ant-oat01-tok"
    sub = _FailingSub(s, "the engine went away mid-turn")
    view = SettingsView(s, _Conns(), subscription=sub)
    assert "off the api meter" not in view._brain_status.text().lower()
    sub.failure = None
    view.reload()
    assert "off the api meter" in view._brain_status.text().lower()


def test_a_subscription_brain_without_the_failure_signal_still_reports_normally(_app):
    # last_failure() is newer than active(); a brain that predates it must degrade to the structural
    # answer, never take the whole Settings page down with an AttributeError.
    s = _Settings()
    s.d["claude_code_oauth_token"] = "sk-ant-oat01-tok"
    sub = _Sub(s, capable=True)
    assert not hasattr(sub, "last_failure")
    view = SettingsView(s, _Conns(), subscription=sub)
    assert "off the api meter" in view._brain_status.text().lower()


def test_talk_while_working_offers_only_the_two_behaviours_that_exist(_app):
    # The console asks one on/off question of narration_mode and the voice layer drops notes that
    # arrive mid-sentence, so a third "every step" option was a promise nothing could keep.
    view = SettingsView(_Settings(), _Conns())
    data = [view._narration.itemData(i) for i in range(view._narration.count())]
    assert data == ["off", "milestones"]


def test_a_retired_narration_choice_still_reads_as_speaking(_app):
    # A machine that saved the dropped "spoken" option has HELIX talking today. An unrecognised value
    # landing on index 0 would silently mute it on upgrade, with nothing to connect that to this page.
    s, c = _Settings(), _Conns()
    s.d["narration_mode"] = "spoken"
    view = SettingsView(s, c)
    assert view._narration.currentData() == "milestones"
    view._save()
    assert s.get("narration_mode") == "milestones"


class _ManagedConns(_Conns):
    """Mirrors the real ConnectionsService for a HELIX-managed key: the secrets store first, then the
    container's getter, which falls through to a legacy Settings entry and finally the environment.

    Pass the Settings store to get the middle rung too — container.py's _tripo_key/_voyage_key/
    _blockade_key read the lower-case spelling (`tripo_api_key`) out of helix_settings.json, which is
    the SAME store this view writes, so a double without it cannot see the copy Remove has to clear."""

    def __init__(self, settings=None):
        super().__init__()
        self._settings = settings

    def value(self, env):
        legacy = str(self._settings.get(env.lower()) or "").strip() if self._settings else ""
        return (self.v.get(env) or "").strip() or legacy or (os.environ.get(env) or "").strip()


def test_remove_says_what_still_holds_a_key_it_cannot_forget(_app, monkeypatch):
    # Remove can only empty the stores this page owns. For an engine key also exported in the
    # environment it never can succeed — and clicking a button whose tooltip promises to forget the
    # key, then watching the dot stay lit with no word, is a dead end.
    shown = []

    class _Box:
        @staticmethod
        def information(_parent, title, body):
            shown.append((title, body))

    monkeypatch.setattr(sv, "QMessageBox", _Box)
    monkeypatch.setenv("TRIPO_API_KEY", "tsk-from-the-environment")
    s, c = _Settings(), _ManagedConns()
    c.set_value("TRIPO_API_KEY", "tsk-pasted")
    view = SettingsView(s, c)
    rows = {sid: (dot, btn) for sid, dot, btn in view._conn_rows}
    dot, remove = rows["tripo"]
    assert dot.text() == "●"
    remove.click()
    assert c.v["TRIPO_API_KEY"] == ""  # our own saved copy really is gone
    assert dot.text() == "●"           # …and the row stays honest: the key is still being used
    assert shown, "Remove silently did nothing about a key it could not forget"
    body = shown[0][1]
    assert "TRIPO_API_KEY" in body and "environment variable" in body


def test_remove_stays_silent_when_it_actually_worked(_app, monkeypatch):
    # The explanation is for the dead end only — a normal Remove must not pop a dialog at anyone.
    shown = []

    class _Box:
        @staticmethod
        def information(*args):
            shown.append(args)

    monkeypatch.setattr(sv, "QMessageBox", _Box)
    monkeypatch.delenv("TRIPO_API_KEY", raising=False)
    s, c = _Settings(), _ManagedConns()
    c.set_value("TRIPO_API_KEY", "tsk-pasted")
    view = SettingsView(s, c)
    rows = {sid: (dot, btn) for sid, dot, btn in view._conn_rows}
    dot, remove = rows["tripo"]
    remove.click()
    assert dot.text() == "○" and not shown


def test_remove_also_clears_the_legacy_engine_key_this_page_wrote_in_an_older_helix(_app, monkeypatch):
    """Remove has to actually remove. V2's Settings page saved the Tripo key as `tripo_api_key` in
    helix_settings.json, and the container still reads it after the secrets store — so clearing only
    the secrets half left the credential on the machine, the dot lit, and HELIX still using it, while
    the dialog told the user it lived outside this page. It doesn't: this page owns that file."""
    shown = []

    class _Box:
        @staticmethod
        def information(*args):
            shown.append(args)

    monkeypatch.setattr(sv, "QMessageBox", _Box)
    monkeypatch.delenv("TRIPO_API_KEY", raising=False)
    s = _Settings()
    c = _ManagedConns(s)
    c.set_value("TRIPO_API_KEY", "tsk-pasted")
    s.d["tripo_api_key"] = "tsk-legacy-from-v2"
    view = SettingsView(s, c)
    rows = {sid: (dot, btn) for sid, dot, btn in view._conn_rows}
    dot, remove = rows["tripo"]
    assert dot.text() == "●"
    remove.click()
    assert c.v["TRIPO_API_KEY"] == ""            # our own saved copy
    assert not s.get("tripo_api_key")            # …and the older HELIX's copy, in this page's file
    assert not c.value("TRIPO_API_KEY")          # nothing resolves it any more — it is really gone
    assert dot.text() == "○" and not remove.isVisibleTo(view)
    assert not shown, f"told the user a key it could have deleted was out of reach: {shown}"


def test_remove_leaves_no_blank_settings_entry_for_a_service_with_no_legacy_copy(_app, monkeypatch):
    # Clearing the legacy spelling unconditionally would write "" for every secrets-store credential
    # on the machine, littering helix_settings.json with slack_token/github_token/... blanks that were
    # never there. Only a legacy entry that actually holds something is touched.
    monkeypatch.setattr(sv, "QMessageBox", type("_B", (), {"information": staticmethod(lambda *a: None)}))
    s, c = _Settings(), _Conns()
    c.set_value("SLACK_TOKEN", "xoxp-1")
    view = SettingsView(s, c)
    rows = {sid: (dot, btn) for sid, dot, btn in view._conn_rows}
    rows["slack"][1].click()
    assert s.d == {}, f"Remove invented Settings entries: {s.d}"


def test_the_stuck_key_message_never_blames_a_store_this_page_can_clear(_app, monkeypatch):
    # The fall-through line is for a credential HELIX genuinely cannot reach. It used to name the
    # older-HELIX Settings entry — which Remove now deletes itself — and told the user their only
    # option was to replace it. A message about a credential has to be exactly true.
    monkeypatch.delenv("TRIPO_API_KEY", raising=False)
    body = SettingsView._stuck_key_text("Tripo", ["TRIPO_API_KEY"]).lower()
    assert "older version" not in body and "connect tripo again" not in body


def test_file_write_toggle_defaults_off_and_round_trips(_app):
    s, c = _Settings(), _Conns()
    view = SettingsView(s, c)
    assert not view._file_write.isChecked()  # writing is opt-in
    view._file_write.setChecked(True)
    view._save()
    assert s.get("file_write_access") is True
    view._file_write.setChecked(False)
    view._save()
    assert s.get("file_write_access") is False
    s.set("file_write_access", True)
    view.reload()
    assert view._file_write.isChecked()


def _wheel(widget):
    """Send one wheel-down tick to a widget and return the event so its accepted flag can be read."""
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent

    ev = QWheelEvent(
        QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(widget, ev)
    return ev


def test_wheel_over_unfocused_combo_scrolls_the_page_not_the_option(_app):
    # Wheeling down the settings page across a combo must not silently change its value; the event
    # is left unaccepted so the scroll area scrolls instead.
    view = SettingsView(_Settings(), _Conns())
    for combo in (view._narration, view._detail, view._voice):
        combo.setCurrentIndex(0)
        ev = _wheel(combo)
        assert combo.currentIndex() == 0  # option untouched
        assert not ev.isAccepted()  # bubbles up to the scroll area
    slider = view._speed
    before = slider.value()
    ev = _wheel(slider)
    assert slider.value() == before and not ev.isAccepted()


def test_wheel_on_clicked_combo_still_changes_it(_app):
    # After a click (focus), the wheel adjusts the control as usual.
    view = SettingsView(_Settings(), _Conns())
    combo = view._narration
    combo.hasFocus = lambda: True  # stands in for a real click — offscreen can't take focus
    combo.setCurrentIndex(0)
    ev = _wheel(combo)
    assert combo.currentIndex() == 1
    assert ev.isAccepted()


def test_audio_device_selection_saves(_app):
    s, c = _Settings(), _Conns()
    view = SettingsView(s, c)
    if view._mic_combo is None or view._out_combo is None:
        pytest.skip("QtMultimedia not available in this environment")
    # Simulate picking specific devices (real device ids aren't present headless) and confirm they persist.
    view._mic_combo.addItem("USB Earphones Mic", "mic-device-id-42")
    view._mic_combo.setCurrentIndex(view._mic_combo.count() - 1)
    view._out_combo.addItem("USB Earphones", "out-device-id-99")
    view._out_combo.setCurrentIndex(view._out_combo.count() - 1)
    view._save()
    assert s.get("audio_input_id") == "mic-device-id-42"
    assert s.get("audio_output_id") == "out-device-id-99"

    # A saved device that isn't currently enumerated is preserved as a placeholder (earphones unplugged),
    # so re-opening Settings and saving doesn't silently drop the choice.
    view.reload()
    assert view._mic_combo.findData("mic-device-id-42") >= 0


# ----- the page is long-lived: opening it must re-read the stores -----

def test_showing_the_page_picks_up_changes_made_elsewhere(_app):
    """The view is built once at startup and reused, so a setting changed by the connect panel, a voice
    toggle or a self-change would otherwise stay invisible until restart."""
    s, c = _Settings(), _Conns()
    view = SettingsView(s, c)
    assert not view._file_write.isChecked()
    assert view._wake_word.text() == ""

    s.set("file_write_access", True)      # e.g. HELIX granted write access mid-conversation
    s.set("wake_word", "Friday")
    c.set_value("SLACK_TOKEN", "xoxp-9")  # pasted into the just-in-time connect panel

    view.show()
    try:
        assert view._file_write.isChecked()
        assert view._wake_word.text() == "Friday"
        dots = {sid: dot for sid, dot, _btn in view._conn_rows}
        assert dots["slack"].text() == "●"
    finally:
        view.hide()


def test_showing_the_page_never_probes_for_a_cli(_app):
    """showEvent runs on the GUI thread on every open — it must not spawn claude.exe to decide."""
    s = _Settings()
    s.d["claude_code_oauth_token"] = "sk-ant-oat01-tok"
    sub = _Sub(s, capable=True)
    view = SettingsView(s, _Conns(), subscription=sub)
    sub.probed.clear()
    view.show()
    try:
        assert sub.probed, "showing the page never refreshed which brain is live"
        assert all(p is False for p in sub.probed), (
            f"SettingsView asked to probe on the GUI thread: {sub.probed}"
        )
    finally:
        view.hide()


def test_reopening_does_not_clobber_text_still_being_typed(_app):
    s = _Settings()
    view = SettingsView(s, _Conns())
    QTest.keyClicks(view._key, "sk-ant-half-typed")   # a real edit, mid-entry, not yet saved
    s.set("claude_api_key", "sk-ant-from-somewhere-else")

    view.show()
    view.hide()
    view.show()
    view.hide()
    assert view._key.text() == "sk-ant-half-typed"    # repeated reloads leave the edit alone

    view._save()
    assert s.get("claude_api_key") == "sk-ant-half-typed"
    # Once saved the field is no longer "mid-entry", so a later external change shows up again.
    s.set("claude_api_key", "sk-ant-rotated")
    view.show()
    try:
        assert view._key.text() == "sk-ant-rotated"
    finally:
        view.hide()


# ----- audio tests must not orphan the device they opened -----

class _FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot=None):
        if not self.slots:
            raise TypeError("signal not connected")  # what PyQt raises; the view must tolerate it
        if slot is None:
            self.slots.clear()
        else:
            self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _FakeDevice:
    @staticmethod
    def isNull():
        return False


class _FakeAudio:
    """Stands in for QAudioSource/QAudioSink: records stop/delete so orphaning is visible to the test."""
    def __init__(self, made, *_a, **_kw):
        self.stopped = False
        self.deleted = False
        self.unparented = False
        self.io = _FakeSignal()          # the readyRead of the QIODevice a source hands back
        self.stateChanged = _FakeSignal()
        made.append(self)

    def start(self, *_a):
        return self

    @property
    def readyRead(self):
        return self.io

    def readAll(self):
        return b""

    def stop(self):
        self.stopped = True

    def setParent(self, _p):
        self.unparented = True

    def deleteLater(self):
        self.deleted = True


def _audio_view(monkeypatch, kind: str):
    """A shown SettingsView whose mic/output test opens a _FakeAudio instead of a real device."""
    view = SettingsView(_Settings(), _Conns())
    if view._mic_combo is None or view._out_combo is None:
        pytest.skip("QtMultimedia not available in this environment")
    made: list[_FakeAudio] = []
    cls = "QAudioSource" if kind == "mic" else "QAudioSink"
    monkeypatch.setattr(sv, cls, lambda *a, **k: _FakeAudio(made, *a, **k), raising=False)
    picker = "_input_device" if kind == "mic" else "_output_device"
    monkeypatch.setattr(SettingsView, picker, staticmethod(lambda _data: _FakeDevice()))
    view.show()
    return view, made


def test_second_mic_test_releases_the_first_microphone(_app, monkeypatch):
    """Clicking Test mic twice used to orphan the first QAudioSource — parented to the page and never
    stopped, it held the mic for the life of the app."""
    view, made = _audio_view(monkeypatch, "mic")
    try:
        view._test_mic()
        view._test_mic()
        assert len(made) == 2
        assert made[0].stopped and made[0].deleted, "the first microphone was left open"
        assert not made[0].io.slots, "the dead source still feeds level readings"
        assert not made[1].stopped and view._mic_src is made[1]
    finally:
        view.hide()
    assert made[1].stopped and made[1].deleted, "leaving the page left the mic open"
    assert view._mic_src is None


def test_a_superseded_mic_timer_does_not_end_the_live_test(_app, monkeypatch):
    view, made = _audio_view(monkeypatch, "mic")
    try:
        view._test_mic()
        stale_gen = view._mic_gen
        view._test_mic()
        view._finish_mic_test(stale_gen)  # the first test's 2s timer fires after the second started
        assert not made[1].stopped and view._mic_src is made[1]
        view._finish_mic_test(view._mic_gen)  # its own timer does end it
        assert made[1].stopped and view._mic_src is None
    finally:
        view.hide()


def test_second_output_test_releases_the_first_sink(_app, monkeypatch):
    view, made = _audio_view(monkeypatch, "out")
    try:
        view._test_output()
        view._test_output()
        assert len(made) == 2
        assert made[0].stopped and made[0].deleted, "the first output device was left open"
        assert not made[0].stateChanged.slots, "a dead sink can still overwrite the status line"
        assert not made[1].stopped and view._test_sink is made[1]
    finally:
        view.hide()
    assert made[1].stopped and made[1].deleted, "leaving the page left the speakers held"
    assert view._test_sink is None and view._test_buf is None


def test_finished_chime_hands_the_output_device_back(_app, monkeypatch):
    view, made = _audio_view(monkeypatch, "out")
    try:
        view._test_output()
        made[0].stateChanged.emit(sv.QAudio.State.IdleState)
        assert made[0].stopped and view._test_sink is None
        assert "heard the chime" in view._out_status.text().lower()
    finally:
        view.hide()
