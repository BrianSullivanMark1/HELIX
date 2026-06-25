"""SettingsView — your Claude API key, and HELIX's voice (neural accent + speed)."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from helix.adapters.speech import DEFAULT_TTS_VOICE, TTS_VOICES, edge_available
from helix.ports.stores import SettingsStore
from helix.ui.theme import MUTED


class SettingsView(QWidget):
    saved = pyqtSignal()

    def __init__(self, settings: SettingsStore) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self._settings = settings

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 24, 36, 28)
        root.setSpacing(12)

        title = QLabel("Settings")
        title.setObjectName("Title")
        root.addWidget(title)

        hint = QLabel(
            "Your Claude API key stays on this machine. The only thing it's ever used for is the "
            "Claude calls you trigger."
        )
        hint.setObjectName("Status")
        hint.setWordWrap(True)
        root.addWidget(hint)

        root.addSpacing(6)
        root.addWidget(QLabel("Claude API key"))
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("sk-ant-…")
        root.addWidget(self._key)

        # Voice (only meaningful when the neural-TTS engine is present).
        self._voice = QComboBox()
        for label, voice_id in TTS_VOICES:
            self._voice.addItem(label, voice_id)
        self._speed = QSlider(Qt.Orientation.Horizontal)
        self._speed.setMinimum(8)   # 0.8×
        self._speed.setMaximum(20)  # 2.0×
        self._speed.setSingleStep(1)
        self._speed_lbl = QLabel("1.0×")
        self._speed_lbl.setMinimumWidth(40)
        self._speed.valueChanged.connect(lambda v: self._speed_lbl.setText(f"{v / 10:.1f}×"))

        self._voice_section: list[QWidget] = []
        if edge_available():
            root.addSpacing(8)
            vlabel = QLabel("HELIX's voice")
            root.addWidget(vlabel)
            vhint = QLabel(
                "Neural voices (online). Falls back to the built-in OS voice if you're offline."
            )
            vhint.setObjectName("Status")
            vhint.setWordWrap(True)
            root.addWidget(vhint)
            root.addWidget(self._voice)
            srow = QHBoxLayout()
            speed_label = QLabel("Speed")
            srow.addWidget(speed_label)
            srow.addWidget(self._speed, stretch=1)
            srow.addWidget(self._speed_lbl)
            root.addLayout(srow)
            self._voice_section = [vlabel, vhint, self._voice, speed_label, self._speed, self._speed_lbl]

        row = QHBoxLayout()
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{MUTED};")
        row.addWidget(save)
        row.addWidget(self._status)
        row.addStretch(1)
        root.addLayout(row)
        root.addStretch(1)

        self.reload()

    def reload(self) -> None:
        self._key.setText(self._settings.get("claude_api_key", "") or "")
        voice = self._settings.get("tts_voice") or DEFAULT_TTS_VOICE
        idx = self._voice.findData(voice)
        self._voice.setCurrentIndex(idx if idx >= 0 else 0)
        try:
            rate = float(self._settings.get("tts_rate"))
        except (TypeError, ValueError):
            rate = 1.0
        self._speed.setValue(int(round(max(0.8, min(2.0, rate)) * 10)))
        self._speed_lbl.setText(f"{self._speed.value() / 10:.1f}×")

    def _save(self) -> None:
        self._settings.set("claude_api_key", self._key.text().strip())
        self._settings.set("tts_voice", self._voice.currentData())
        self._settings.set("tts_rate", round(self._speed.value() / 10, 1))
        self._status.setText("Saved.")
        self.saved.emit()
