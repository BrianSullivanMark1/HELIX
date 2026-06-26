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

        # Tripo API key — enables film-quality (neural) 3D models. Optional; without it, models use the
        # local primitive builder. Lives on this machine; only sent to Tripo when you build a model.
        root.addSpacing(8)
        root.addWidget(QLabel("Tripo API key (high-detail 3D models)"))
        thint = QLabel(
            "Optional. Enables recognizable, film-quality 3D models (your description is sent to Tripo "
            "to generate them). Get a key at platform.tripo3d.ai. Without it, models use basic shapes."
        )
        thint.setObjectName("Status")
        thint.setWordWrap(True)
        root.addWidget(thint)
        self._tripo = QLineEdit()
        self._tripo.setEchoMode(QLineEdit.EchoMode.Password)
        self._tripo.setPlaceholderText("tsk_…")
        root.addWidget(self._tripo)

        # 3D model detail — High keeps native polygon counts + detailed textures (no forced low-poly).
        root.addSpacing(8)
        root.addWidget(QLabel("3D model detail"))
        dhint = QLabel(
            "High = native polygon count and detailed textures — best quality, heavier to render. "
            "Balanced is lighter and faster, and renders on any machine."
        )
        dhint.setObjectName("Status")
        dhint.setWordWrap(True)
        root.addWidget(dhint)
        self._detail = QComboBox()
        self._detail.addItem("Balanced — faster, lighter", "balanced")
        self._detail.addItem("High — native poly + detailed textures", "high")
        root.addWidget(self._detail)

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
        self._tripo.setText(self._settings.get("tripo_api_key", "") or "")
        detail = (self._settings.get("model_detail") or "balanced").lower()
        didx = self._detail.findData(detail)
        self._detail.setCurrentIndex(didx if didx >= 0 else 0)
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
        self._settings.set("tripo_api_key", self._tripo.text().strip())
        self._settings.set("model_detail", self._detail.currentData())
        self._settings.set("tts_voice", self._voice.currentData())
        self._settings.set("tts_rate", round(self._speed.value() / 10, 1))
        self._status.setText("Saved.")
        self.saved.emit()
