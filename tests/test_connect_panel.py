"""The just-in-time connect panel — values land in the right store; unknown ids show nothing."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit  # noqa: E402

from helix.domain.connections import Connection  # noqa: E402
from helix.services.connections import CONNECTABLE  # noqa: E402
from helix.ui.connections_dialog import (  # noqa: E402
    ConnectionsDialog,
    ConnectPanel,
    show_connect_panel,
)


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


def test_a_pasted_url_warns_once_then_saves_only_if_the_user_insists(_app, monkeypatch):
    # The SAM.gov mis-paste: the API's endpoint URL pasted where the key belongs. First Connect
    # holds the save and warns; an unchanged second click means the user insists, so it saves.
    conns, settings = _Conns(), _Settings()
    (panel,) = _open(monkeypatch, "sam", "to watch procurement", conns, settings)
    panel._fields[0][1].setText("https://api.sam.gov/opportunities/v2/search")
    panel._connect_btn.click()
    assert conns.v == {}                                  # held — nothing saved yet
    assert panel.result() == 0                            # panel stays open
    assert "web address" in panel._status.text()
    panel._connect_btn.click()                            # unchanged value → the user insists
    assert conns.value("SAM_API_KEY") == "https://api.sam.gov/opportunities/v2/search"
    assert panel.result() == 1

    # a corrected value after the warning saves immediately, no second insist needed
    conns2, settings2 = _Conns(), _Settings()
    (panel2,) = _open(monkeypatch, "sam", "", conns2, settings2)
    panel2._fields[0][1].setText("https://api.sam.gov/x")
    panel2._connect_btn.click()
    assert conns2.v == {}
    panel2._fields[0][1].setText("real-key-123")
    panel2._connect_btn.click()
    assert conns2.value("SAM_API_KEY") == "real-key-123"
    assert panel2.result() == 1


def test_a_url_typed_field_is_exempt_from_the_mispaste_guard(_app, monkeypatch):
    # A field whose own naming says its value IS a URL (a webhook, SUPABASE_URL) must save a URL
    # on the first click — 'that looks like a web address' would be actively wrong advice there.
    conns, settings = _Conns(), _Settings()
    monkeypatch.setitem(
        CONNECTABLE, "hookco",
        ("HookCo", "secrets", (("HOOKCO_WEBHOOK_URL", "HookCo webhook URL", "https://…"),)),
    )
    (panel,) = _open(monkeypatch, "hookco", "", conns, settings)
    panel._fields[0][1].setText("https://hooks.example.com/T123/B456")
    panel._connect_btn.click()
    assert conns.value("HOOKCO_WEBHOOK_URL") == "https://hooks.example.com/T123/B456"
    assert panel.result() == 1  # saved first click, no warning hold


def test_build_dialog_never_re_warns_on_a_stored_unedited_value(_app):
    # The guard scans only what the user CHANGED this session: a stored URL-shaped value (however it
    # got there) prefills the dialog, and saving an edit to a DIFFERENT field must not be held
    # hostage by a warning about a field the user never touched.
    store = {"SOME_ENDPOINT": "https://xyz.supabase.co", "SLACK_TOKEN": ""}
    dlg = ConnectionsDialog(
        None, "Connect", [Connection("SOME_ENDPOINT", "Endpoint"), Connection("SLACK_TOKEN", "Slack")],
        get_value=lambda k: store.get(k, ""), set_value=store.__setitem__,
    )
    dlg._fields[1][1].setText("xoxp-new-token")
    dlg._save()
    assert store["SLACK_TOKEN"] == "xoxp-new-token"  # saved on the FIRST click
    assert dlg.result() == 1

    # but a freshly TYPED URL in a key field still warns first, and an unchanged second click insists
    store2 = {"API_KEY": ""}
    dlg2 = ConnectionsDialog(
        None, "Connect", [Connection("API_KEY", "API key")],
        get_value=lambda k: store2.get(k, ""), set_value=store2.__setitem__,
    )
    dlg2._fields[0][1].setText("api.sam.gov/opportunities/v2/search")
    dlg2._save()
    assert store2["API_KEY"] == "" and dlg2.result() == 0
    assert "web address" in dlg2._status.text()
    dlg2._save()
    assert store2["API_KEY"] == "api.sam.gov/opportunities/v2/search" and dlg2.result() == 1


def test_fields_are_masked_hinted_and_the_reason_is_shown(_app, monkeypatch):
    conns, settings = _Conns(), _Settings()
    (panel,) = _open(monkeypatch, "github", "to check your pull requests", conns, settings)
    _key, field = panel._fields[0]
    assert field.echoMode() == QLineEdit.EchoMode.Password  # never plaintext
    assert field.placeholderText()                          # the hint guides the paste
    texts = [w.text() for w in panel.findChildren(QLabel)]
    assert "to check your pull requests" in texts
    assert panel.result() == 0  # still open until Connect/Cancel


def test_saving_the_build_panel_never_copies_a_helix_managed_key_into_the_secrets_store(_app):
    # A build that needs AI declares ANTHROPIC_API_KEY, and ConnectionsService.value() serves it from
    # Settings — so the panel prefills with the user's real Claude key. Writing that back would COPY it
    # into the secrets store, which value() prefers forever after: rotating the key in Settings would
    # then never reach this build again. Only what the user CHANGED may be written.
    secrets: dict[str, str] = {}                       # what the Save actually writes
    managed = {"ANTHROPIC_API_KEY": "sk-ant-live"}     # lives in Settings, not here

    def get_value(k):
        return secrets.get(k) or managed.get(k, "")

    dlg = ConnectionsDialog(
        None, "Connect — Notes App",
        [Connection("ANTHROPIC_API_KEY", "Claude (AI)"), Connection("SLACK_TOKEN", "Slack token")],
        get_value=get_value, set_value=secrets.__setitem__,
        is_managed=lambda k: k in managed,
    )
    dlg._fields[1][1].setText("xoxp-typed-now")
    dlg._save()
    assert secrets == {"SLACK_TOKEN": "xoxp-typed-now"}  # the managed key was NOT snapshotted
    assert get_value("ANTHROPIC_API_KEY") == "sk-ant-live"
    managed["ANTHROPIC_API_KEY"] = "sk-ant-rotated"      # rotating in Settings still reaches the build
    assert get_value("ANTHROPIC_API_KEY") == "sk-ant-rotated"


def test_a_managed_row_says_it_is_already_connected_and_an_override_can_be_cleared(_app):
    # The user needs to see WHY a filled field wasn't asked for, and an override (including one the
    # old copy-on-save left behind) must stay visible and clearable — emptying the box is a change, so
    # it writes "" and the build falls back to HELIX's own key.
    secrets = {"ANTHROPIC_API_KEY": "sk-ant-stale-override"}
    dlg = ConnectionsDialog(
        None, "Connect — Notes App", [Connection("ANTHROPIC_API_KEY", "Claude (AI)", "sk-ant-…")],
        get_value=lambda k: secrets.get(k, ""), set_value=secrets.__setitem__,
        is_managed=lambda k: k == "ANTHROPIC_API_KEY",
    )
    labels = [w.text() for w in dlg.findChildren(QLabel)]
    assert any("already connected in HELIX" in t for t in labels)
    assert not any("sk-ant-…" in t for t in labels)  # the managed note replaces the paste-me hint
    dlg._fields[0][1].setText("")
    dlg._save()
    assert secrets["ANTHROPIC_API_KEY"] == ""  # cleared, so value() goes back to the Settings key


@pytest.mark.parametrize("held", ["", "   "])
def test_a_managed_key_holding_nothing_is_not_announced_as_already_connected(_app, held):
    """is_managed answers "HELIX would supply this key", not "HELIX has one". The container registers
    ANTHROPIC/CLAUDE/TRIPO/VOYAGE/BLOCKADE unconditionally, so a subscription-only install — no
    claude_api_key saved anywhere — hit the managed branch with an EMPTY box and was told to leave it
    as is, with its own paste-me hint replaced by that advice. The launcher card that opened this
    panel was lit amber "Connect" off the very same empty value: two surfaces, one credential,
    opposite claims. The note is a claim of possession, so it must be gated on the value."""
    dlg = ConnectionsDialog(
        None, "Connect — Notes App", [Connection("ANTHROPIC_API_KEY", "Claude (AI)", "sk-ant-…")],
        get_value=lambda k: held, set_value=lambda k, v: None,
        is_managed=lambda k: True,
    )
    labels = [w.text() for w in dlg.findChildren(QLabel)]
    assert not any("already connected" in t for t in labels), labels
    assert any("sk-ant-…" in t for t in labels), labels  # the hint that says what to paste survives
    assert not dlg._fields[0][1].text().strip()          # the box really is blank
