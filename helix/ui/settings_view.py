"""SettingsView — clean, scannable settings.

One required field (your Claude key) up top; every OPTIONAL integration grouped under "API Connections",
each a single row with an at-a-glance Set/Not-set status and an ⓘ button that pops the detail — so the
page stays uncluttered and easy to fill in. Long forms scroll; Save stays pinned at the bottom.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from helix.adapters.speech import DEFAULT_TTS_VOICE, TTS_VOICES, edge_available
from helix.domain.connections import KNOWN_SERVICES
from helix.ports.stores import SettingsStore
from helix.ui.theme import CYAN, LINE, MUTED, STATUS_DONE


class SettingsView(QWidget):
    saved = pyqtSignal()

    def __init__(self, settings: SettingsStore, connections=None) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self._settings = settings
        self._connections = connections           # save/load service tokens used by agents + call_api
        self._conn_fields: list[tuple[str, QLineEdit]] = []  # (env-var name, field)
        # (status QLabel, getter) pairs refreshed on load + save so each row shows Set / Not set at a glance.
        self._statuses: list[tuple[QLabel, Callable[[], str]]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 22, 36, 18)
        root.setSpacing(10)

        title = QLabel("Settings")
        title.setObjectName("Title")
        root.addWidget(title)

        # Everything scrolls; Save stays pinned below, so a long form never hides the button.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_host = QWidget()
        form_host.setStyleSheet("background:transparent;")
        form = QVBoxLayout(form_host)
        form.setContentsMargins(0, 2, 12, 4)
        form.setSpacing(8)

        # ── Required: the Claude key ──
        form.addWidget(self._section("HELIX", "Your key stays on this machine — used only for the Claude calls you trigger."))
        self._key = self._password("sk-ant-…")
        form.addWidget(self._field_row(
            "Claude API key", "Claude API key",
            "Powers everything HELIX does — the conversation and the apps it builds. Get one at "
            "console.anthropic.com. It's stored locally and only ever used for the Claude calls you make.",
            self._key, lambda: self._settings.get("claude_api_key", "") or "",
        ))

        # ── Optional integrations, grouped ──
        form.addSpacing(4)
        form.addWidget(self._section(
            "API Connections", "Optional. Add a key to unlock a feature — HELIX works fine without these."
        ))
        self._tripo = self._password("tsk_…")
        form.addWidget(self._field_row(
            "Tripo — high-detail 3D models", "Tripo API key",
            "Optional. Turns build_3d_model into recognizable, film-quality 3D (your description is sent to "
            "Tripo to generate the mesh). Get a key at platform.tripo3d.ai. Without it, models use the "
            "built-in basic-shape builder.",
            self._tripo, lambda: self._settings.get("tripo_api_key", "") or "",
        ))
        self._voyage = self._password("pa-…")
        form.addWidget(self._field_row(
            "Voyage — smarter knowledge search", "Voyage API key",
            "Optional. Lets HELIX search your saved knowledge by MEANING, not just matching words (e.g. "
            "“how do I get in?” finds a note about the door code). Get a key at voyageai.com. Without it, "
            "knowledge search still works using keywords.",
            self._voyage, lambda: self._settings.get("voyage_api_key", "") or "",
        ))
        # Service tokens (Slack, GitHub, …) your apps and agents read, or the orb's read-only call_api uses.
        if self._connections is not None:
            for svc in KNOWN_SERVICES:
                field = self._password(svc.hint)
                self._conn_fields.append((svc.env, field))
                form.addWidget(self._field_row(
                    f"{svc.label}", f"{svc.label} token",
                    f"Optional. Lets apps, flows, and agents you build read your {svc.label} account, and "
                    f"the orb answer questions about it. Stored on this machine only; never written into a "
                    f"build's files or sent anywhere except {svc.label}. Format: {svc.hint}.",
                    field, (lambda env=svc.env: self._connections.value(env) or ""),
                ))

        # ── Appearance & voice ──
        form.addSpacing(4)
        form.addWidget(self._section("Appearance & voice"))
        self._detail = QComboBox()
        self._detail.addItem("Balanced — faster, lighter", "balanced")
        self._detail.addItem("High — native poly + detailed textures", "high")
        form.addWidget(self._labeled(
            "3D model detail", "3D model detail",
            "High keeps native polygon counts and detailed textures — best quality, heavier to render. "
            "Balanced is lighter and renders on any machine.",
            self._detail,
        ))

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
        if edge_available():
            form.addWidget(self._labeled(
                "HELIX's voice", "HELIX's voice",
                "Neural voices (online). Falls back to the built-in OS voice if you're offline.",
                self._voice,
            ))
            srow = QWidget()
            sl = QHBoxLayout(srow)
            sl.setContentsMargins(0, 0, 0, 0)
            sl.addWidget(QLabel("Speed"))
            sl.addWidget(self._speed, stretch=1)
            sl.addWidget(self._speed_lbl)
            form.addWidget(srow)

        form.addStretch(1)
        scroll.setWidget(form_host)
        root.addWidget(scroll, stretch=1)

        # Save row — pinned.
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

        self.reload()

    # ----- small builders -----
    @staticmethod
    def _password(placeholder: str) -> QLineEdit:
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText(placeholder)
        return field

    @staticmethod
    def _section(title: str, subtitle: str | None = None) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(2)
        head = QLabel(title.upper())
        head.setStyleSheet(f"color:{CYAN};font-size:12px;font-weight:700;letter-spacing:1px;")
        lay.addWidget(head)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color:{MUTED};font-size:12px;")
            lay.addWidget(sub)
        # a hairline under the header
        rule = QLabel()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background:{LINE};")
        lay.addWidget(rule)
        return box

    def _info_btn(self, title: str, body: str) -> QToolButton:
        # A small circular "?" badge — press it for the detail (kept out of the page). Plain "?" so it
        # renders on every machine/font (a circled-i glyph tofu'd in testing).
        btn = QToolButton()
        btn.setText("?")
        btn.setFixedSize(20, 20)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("What's this?")
        btn.setStyleSheet(
            "QToolButton{border:1px solid #2a3a44;border-radius:10px;background:transparent;"
            "color:#7a8a93;font-size:12px;font-weight:700;}"
            "QToolButton:hover{color:#3fe0e0;border-color:#3fe0e0;}"
        )
        btn.clicked.connect(lambda: QMessageBox.information(self, title, body))
        return btn

    def _field_row(self, name: str, info_title: str, info_body: str, field: QLineEdit,
                   getter: Callable[[], str]) -> QWidget:
        """A labelled secret field: [name] [Set/Not set] … [ⓘ], with the field below. Detail is in the ⓘ
        popup, never crowding the page."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        lbl = QLabel(name)
        lbl.setStyleSheet("font-weight:600;")
        status = QLabel()
        status.setStyleSheet("font-size:12px;")
        head.addWidget(lbl)
        head.addWidget(status)
        head.addStretch(1)
        head.addWidget(self._info_btn(info_title, info_body))
        lay.addLayout(head)
        lay.addWidget(field)
        self._statuses.append((status, getter))
        return box

    def _labeled(self, name: str, info_title: str, info_body: str, widget: QWidget) -> QWidget:
        """A labelled control (combo/etc.) with an ⓘ for the detail and the control below."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        lbl = QLabel(name)
        lbl.setStyleSheet("font-weight:600;")
        head.addWidget(lbl)
        head.addStretch(1)
        head.addWidget(self._info_btn(info_title, info_body))
        lay.addLayout(head)
        lay.addWidget(widget)
        return box

    def _refresh_statuses(self) -> None:
        for label, getter in self._statuses:
            try:
                has = bool((getter() or "").strip())
            except Exception:  # noqa: BLE001
                has = False
            label.setText("● Set" if has else "○ Not set")
            label.setStyleSheet(f"font-size:12px;color:{STATUS_DONE if has else MUTED};")

    # ----- load / save -----
    def reload(self) -> None:
        self._key.setText(self._settings.get("claude_api_key", "") or "")
        self._tripo.setText(self._settings.get("tripo_api_key", "") or "")
        self._voyage.setText(self._settings.get("voyage_api_key", "") or "")
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
        if self._connections is not None:
            for env, field in self._conn_fields:
                field.setText(self._connections.value(env) or "")
        self._refresh_statuses()

    def _save(self) -> None:
        self._settings.set("claude_api_key", self._key.text().strip())
        self._settings.set("tripo_api_key", self._tripo.text().strip())
        self._settings.set("voyage_api_key", self._voyage.text().strip())
        self._settings.set("model_detail", self._detail.currentData())
        self._settings.set("tts_voice", self._voice.currentData())
        self._settings.set("tts_rate", round(self._speed.value() / 10, 1))
        if self._connections is not None:
            for env, field in self._conn_fields:
                self._connections.set_value(env, field.text().strip())
        self._refresh_statuses()
        self._status.setText("Saved.")
        self.saved.emit()
