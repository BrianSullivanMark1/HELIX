"""SettingsView — the redesigned settings: a save round-trip and the at-a-glance Set/Not-set statuses."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

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
    view._voyage.setText("pa-xyz")
    view._tripo.setText("tsk-123")
    view._conn_fields[0][1].setText("xoxp-token")  # first known service (Slack)
    view._save()

    assert s.get("claude_api_key") == "sk-ant-abc"
    assert s.get("voyage_api_key") == "pa-xyz"
    assert s.get("tripo_api_key") == "tsk-123"
    assert c.value(view._conn_fields[0][0]) == "xoxp-token"


def test_status_pills_reflect_set_state(_app):
    s, c = _Settings(), _Conns()
    s.d["claude_api_key"] = "sk-ant-present"
    view = SettingsView(s, c)  # reload() runs in __init__
    # the Claude key status (first registered) shows Set; an unset optional key shows Not set
    labels = [lbl.text() for lbl, _ in view._statuses]
    assert "● Set" in labels and "○ Not set" in labels
