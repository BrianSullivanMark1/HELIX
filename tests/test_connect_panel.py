"""The just-in-time connect panel — values land in the right store; unknown ids show nothing."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit  # noqa: E402

from helix.services.connections import CONNECTABLE  # noqa: E402
from helix.ui.connections_dialog import ConnectPanel, show_connect_panel  # noqa: E402


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


def _open(monkeypatch, service_id, reason, conns, settings):
    """Run show_connect_panel with exec stubbed out and hand back any panel it built."""
    shown: list[ConnectPanel] = []
    monkeypatch.setattr(ConnectPanel, "exec", lambda self: shown.append(self) or 0)
    show_connect_panel(None, service_id, reason, connections=conns, settings=settings)
    return shown


def test_connect_writes_the_secrets_store_and_closes(_app, monkeypatch):
    conns, settings = _Conns(), _Settings()
    (panel,) = _open(monkeypatch, "slack", "so I can watch your channels", conns, settings)
    assert panel.windowTitle() == "Connect Slack"
    key, field = panel._fields[0]
    assert key == "SLACK_TOKEN"
    field.setText("  xoxp-secret  ")
    panel._connect_btn.click()
    assert conns.value("SLACK_TOKEN") == "xoxp-secret"  # trimmed, in the SECRETS store
    assert settings.d == {}                             # never the settings store
    assert panel.result() == 1                          # accepted → the panel closed


def test_engine_keys_land_in_the_secrets_store(_app, monkeypatch):
    conns, settings = _Conns(), _Settings()
    (panel,) = _open(monkeypatch, "tripo", "", conns, settings)
    panel._fields[0][1].setText("tsk_123")
    panel._connect_btn.click()
    assert conns.value("TRIPO_API_KEY") == "tsk_123"  # guard-safe: never the settings file
    assert settings.d == {}


def test_a_settings_store_service_writes_settings(_app, monkeypatch):
    conns, settings = _Conns(), _Settings()
    monkeypatch.setitem(
        CONNECTABLE, "fakeco", ("FakeCo", "settings", (("fake_api_key", "FakeCo key", "fk-…"),))
    )
    (panel,) = _open(monkeypatch, "fakeco", "", conns, settings)
    panel._fields[0][1].setText("fk-1")
    panel._connect_btn.click()
    assert settings.get("fake_api_key") == "fk-1"  # a settings-store service lands in Settings
    assert conns.v == {}


def test_unknown_service_shows_nothing(_app, monkeypatch):
    conns, settings = _Conns(), _Settings()
    assert _open(monkeypatch, "not-a-service", "", conns, settings) == []
    assert conns.v == {} and settings.d == {}


def test_an_empty_field_writes_nothing(_app, monkeypatch):
    conns, settings = _Conns(), _Settings()
    (panel,) = _open(monkeypatch, "alpaca", "", conns, settings)
    assert [k for k, _f in panel._fields] == ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]
    panel._fields[0][1].setText("AK-only")  # the secret field stays untouched
    panel._connect_btn.click()
    assert conns.value("ALPACA_API_KEY") == "AK-only"
    assert "ALPACA_SECRET_KEY" not in conns.v  # an empty field never writes (or clears) a value


def test_fields_are_masked_hinted_and_the_reason_is_shown(_app, monkeypatch):
    conns, settings = _Conns(), _Settings()
    (panel,) = _open(monkeypatch, "github", "to check your pull requests", conns, settings)
    _key, field = panel._fields[0]
    assert field.echoMode() == QLineEdit.EchoMode.Password  # never plaintext
    assert field.placeholderText()                          # the hint guides the paste
    texts = [w.text() for w in panel.findChildren(QLabel)]
    assert "to check your pull requests" in texts
    assert panel.result() == 0  # still open until Connect/Cancel
