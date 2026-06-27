"""CommandsDialog — the Forge's keyword/control reference renders and covers the real actions."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.ui.commands_view import COMMAND_GROUPS, CommandsDialog  # noqa: E402


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


def test_commands_dialog_renders(_app):
    dlg = CommandsDialog()
    dlg.resize(560, 620)
    assert not dlg.grab().isNull()


def test_groups_cover_the_real_actions():
    flat = "\n".join(f"{k} {a}" for _, rows in COMMAND_GROUPS for k, a in rows).lower()
    for kw in ("helix", "mute", "unmute", "stop build", "rename", "delete", "run my", "improve helix",
               "attach", "goodbye"):
        assert kw in flat, kw


def test_every_action_is_one_clean_sentence():
    for _, rows in COMMAND_GROUPS:
        for keys, action in rows:
            assert keys and action
            assert "\n" not in action  # one line
            assert action[0].isupper() and action.endswith(".")  # a single sentence
