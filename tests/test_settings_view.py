"""SettingsView — the save round-trip, the slim review-only Connections manager, the statuses."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.services.connections import CONNECTABLE  # noqa: E402
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


def test_evolve_defaults_on_and_round_trips(_app):
    s, c = _Settings(), _Conns()
    view = SettingsView(s, c)
    assert view._evolve.isChecked()  # a missing key means ON
    view._evolve.setChecked(False)
    view._save()
    assert s.get("evolve_enabled") is False
    s.set("evolve_enabled", True)
    view.reload()
    assert view._evolve.isChecked()


class _Sub:
    """Mirrors SubscriptionBrain.active(): live only when a token is saved AND the engine is present."""
    def __init__(self, settings, capable=True):
        self._settings = settings
        self._capable = capable  # SDK + Claude Code CLI reachable

    def active(self):
        token = (self._settings.get("claude_code_oauth_token") or "").strip()
        return self._capable and bool(token)


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
