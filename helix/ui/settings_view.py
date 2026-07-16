"""SettingsView — clean, scannable settings.

One required field (your Claude key) up top. Service keys are NOT entered here: they're pasted into
the just-in-time connect panel the orb opens mid-conversation, so Connections is a slim review-only
manager — one row per service with a connected dot and a Remove. Long forms scroll; Save stays
pinned at the bottom.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
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
from helix.ports.stores import SettingsStore
from helix.services.connections import CONNECTABLE
from helix.services.files import WRITE_ACCESS_KEY
from helix.ui.theme import CYAN, LINE, MUTED, STATUS_DONE
from helix.ui.voice import AUDIO_INPUT_SETTING, AUDIO_OUTPUT_SETTING, device_id_str

try:  # QtMultimedia ships with PyQt6 but needs platform plugins; degrade to no device pickers if absent.
    from PyQt6.QtMultimedia import (
        QAudio,
        QAudioFormat,
        QAudioSink,
        QAudioSource,
        QMediaDevices,
    )

    _AUDIO = True
except Exception:  # pragma: no cover - depends on the host's Qt plugins
    _AUDIO = False


def _chime_pcm(sample_rate: int, channels: int = 1) -> bytes:
    """A short, click-free two-tone chime as 16-bit mono/stereo PCM — the 'test sound' for an output."""
    import array
    import math

    def tone(freq: float, ms: int, vol: float = 0.4) -> array.array:
        n = int(sample_rate * ms / 1000)
        attack, release = 0.02 * sample_rate, 0.05 * sample_rate
        buf = array.array("h")
        for i in range(n):
            env = min(1.0, i / max(1.0, attack), (n - i) / max(1.0, release))
            s = int(vol * env * 32767 * math.sin(2 * math.pi * freq * i / sample_rate))
            s = max(-32768, min(32767, s))
            for _ in range(max(1, channels)):
                buf.append(s)
        return buf

    out = array.array("h")
    out += tone(659.25, 170)  # E5
    out += tone(880.0, 260)   # A5
    return out.tobytes()


def _peak_rms(pcm: bytes) -> float:
    """Peak RMS of 16-bit LE mono PCM (stdlib only) — how loud the test mic heard you."""
    import array
    import math

    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class SettingsView(QWidget):
    saved = pyqtSignal()

    def __init__(self, settings: SettingsStore, connections=None, gmail=None, calendar=None,
                 subscription=None, memory=None, remote=None) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self._settings = settings
        self._subscription = subscription          # SubscriptionBrain — reports which brain is live
        self._memory = memory                      # MemoryService — the "Manage long-term memory" browser
        self._remote = remote                      # RemoteService — the optional phone companion
        self._connections = connections           # review/remove tokens (set via the connect panel)
        self._gmail = gmail                        # read-only Gmail inbox credentials (address + app password)
        self._calendar = calendar                  # read-only calendar (the private iCal URL is the secret)
        self._conn_rows: list[tuple[str, QLabel, QPushButton]] = []  # (service id, dot, Remove)
        # (status QLabel, getter) pairs refreshed on load + save so each row shows Set / Not set at a glance.
        self._statuses: list[tuple[QLabel, Callable[[], str]]] = []
        # Audio device pickers (mic + output). Only built when QtMultimedia loaded; refs kept for save/test.
        self._mic_combo: QComboBox | None = None
        self._out_combo: QComboBox | None = None
        self._mic_src = None  # live QAudioSource during a mic test
        self._mic_io = None
        self._mic_peak = 0.0
        self._test_sink = None  # live QAudioSink during an output test (kept alive so it isn't GC'd)
        self._test_buf = None

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

        # ── Required: connect Claude (one of the two) ──
        form.addWidget(self._section(
            "HELIX", "Connect Claude one of two ways — both stay on this machine. With BOTH set, the "
            "subscription token is preferred and the API key becomes the fallback."
        ))
        self._oauth_token = self._password("sk-ant-oat01-…")
        form.addWidget(self._field_row(
            "Claude subscription token", "Claude subscription token (Claude Code)",
            "RECOMMENDED — runs HELIX's conversation, watchers, and app-building on your Claude "
            "Pro/Max subscription (the same usage pool as Claude Desktop) instead of pay-per-token "
            "API billing. One-time setup: open a terminal, run: claude setup-token — approve in the "
            "browser, then paste the token here. Note: apps you build that make their OWN Claude "
            "calls still need the API key below.",
            self._oauth_token, lambda: self._settings.get("claude_code_oauth_token", "") or "",
        ))
        self._key = self._password("sk-ant-…")
        form.addWidget(self._field_row(
            "Claude API key", "Claude API key",
            "Alternative to the subscription token — bills per token at console.anthropic.com. Also "
            "acts as the fallback when a subscription turn fails (expired token, plan limits). It's "
            "stored locally and only ever used for the Claude calls you make.",
            self._key, lambda: self._settings.get("claude_api_key", "") or "",
        ))
        # Live "which brain is running" line — so it's never a mystery whether HELIX is drawing on the
        # subscription or the metered API. Reflects SAVED settings + real capability (token present AND
        # the Claude Code app reachable), refreshed on open and after Save.
        self._brain_status = QLabel()
        self._brain_status.setWordWrap(True)
        self._brain_status.setContentsMargins(2, 2, 2, 0)
        form.addWidget(self._brain_status)
        self._refresh_brain_status()
        # Evolve — the overnight self-improvement drafter. It only ever DRAFTS; applying a change is
        # always a separate, explicit approval, so the switch is safe to ship on by default.
        self._evolve = QCheckBox(
            "Evolve — draft one self-improvement overnight (never applies itself)"
        )
        form.addWidget(self._check_row(
            self._evolve, "Evolve",
            "Once a night, HELIX drafts ONE improvement to itself — a proposal you can read, then "
            "approve or discard. It NEVER applies a change on its own; approving is always a "
            "separate, explicit step. Turn this off to stop the overnight drafting entirely.",
        ))

        # ── Connections — review-only. Keys are pasted into the just-in-time connect panel ──
        form.addSpacing(4)
        form.addWidget(self._section(
            "Connections",
            "Keys are added in conversation — ask HELIX to connect a service and a secure panel "
            "opens.",
        ))
        form.addWidget(self._connections_manager())
        # Gmail — read-only inbox (an address + a Google App Password). Two fields, one status.
        if self._gmail is not None:
            self._gmail_addr = QLineEdit()
            self._gmail_addr.setPlaceholderText("you@gmail.com")
            self._gmail_pw = self._password("16-character app password")
            form.addWidget(self._gmail_section())
        # Calendar — read-only (one private iCal URL; the URL itself is the credential).
        if self._calendar is not None:
            self._calendar_url = self._password("https://calendar.google.com/calendar/ical/…/basic.ics")
            form.addWidget(self._calendar_section())

        # ── Files on this PC — reading is always on; WRITING is this switch ──
        form.addSpacing(4)
        form.addWidget(self._section(
            "Files on this PC",
            "HELIX can always READ folders and files you ask about. Writing files is off unless you "
            "turn it on here.",
        ))
        self._file_write = QCheckBox("Allow HELIX to write files")
        fw_row = QWidget()
        fw_lay = QHBoxLayout(fw_row)
        fw_lay.setContentsMargins(0, 2, 0, 2)
        fw_lay.setSpacing(8)
        fw_lay.addWidget(self._file_write)
        fw_lay.addStretch(1)
        fw_lay.addWidget(self._info_btn(
            "Files on this PC",
            "READING is always available: ask HELIX what's in a folder or to read a file (text, "
            "code, PDF, Word) and it answers from it.\n\n"
            "WRITING is this switch. When ON, HELIX can create files on this PC when you ask it to, "
            "and it always asks before replacing an existing file. When OFF, HELIX never writes.\n\n"
            "Either way, HELIX can never read your saved keys and secrets, and can never write into "
            "its own program or data folders — so it can't change itself or your built apps through "
            "this. Takes effect immediately; no restart needed.",
        ))
        form.addWidget(fw_row)

        # ── Conversation & presence — how HELIX talks, listens, and speaks up on its own ──
        form.addSpacing(4)
        form.addWidget(self._section(
            "Conversation & presence",
            "How HELIX listens, how much it says out loud, and when it speaks up on its own.",
        ))
        self._wake_word = QLineEdit()
        self._wake_word.setPlaceholderText("HELIX")
        form.addWidget(self._labeled(
            "Wake word", "Wake word",
            "The name you say to get HELIX's attention hands-free. Pick a word your household doesn't say "
            "all day — useful if “HELIX” (or words like “stop”/“goodbye”) keep triggering it around kids "
            "or the TV. Try something distinctive like “Athena” or “Friday”. Leave blank for the default "
            "“HELIX”. Inside a short back-and-forth you don't need to repeat it.",
            self._wake_word,
        ))
        self._narration = QComboBox()
        self._narration.addItem("Stay quiet while working (recommended)", "off")
        self._narration.addItem("Speak milestones", "milestones")
        self._narration.addItem("Speak every step", "spoken")
        form.addWidget(self._labeled(
            "Talk while working", "Talk while working",
            "Whether HELIX narrates its progress out loud while it builds. Quiet keeps progress on the "
            "screen and in the orb's colour (nothing is lost — you still see every step); the other "
            "options speak it aloud.",
            self._narration,
        ))
        self._proactive = QCheckBox("Let background watchers speak up out loud")
        form.addWidget(self._check_row(
            self._proactive, "Background updates",
            "HELIX quietly watches things in the background (GitHub, Slack, your portfolio, and so on). "
            "When OFF, anything they find is shown in the conversation but NOT spoken — so HELIX isn't "
            "talking at the room all day. When ON, notable updates are also read aloud. Reminders you set "
            "yourself are always spoken either way.",
        ))
        self._trust_voice = QCheckBox("Single-user home — trust any voice")
        form.addWidget(self._check_row(
            self._trust_voice, "Trust any voice",
            "By default, once you register your voice, HELIX only acts on voices it recognizes and asks "
            "an unknown voice to register. Turn this ON in a single-user home to skip that entirely — "
            "HELIX will act on whoever speaks and never ask “who's this?”. Leave it OFF if you want "
            "HELIX to ignore voices it doesn't know.",
        ))
        if self._memory is not None:
            mem_btn = QPushButton("🧠 Manage long-term memory…")
            mem_btn.clicked.connect(self._open_memory)
            mrow = QHBoxLayout()
            mrow.setContentsMargins(0, 2, 0, 2)
            mrow.addWidget(mem_btn)
            mrow.addStretch(1)
            mem_host = QWidget()
            mem_host.setLayout(mrow)
            form.addWidget(mem_host)

        # ── Remote access (phone) — OFF by default; a token-gated local companion ──
        if self._remote is not None:
            form.addSpacing(4)
            form.addWidget(self._section(
                "Remote access (phone)",
                "Off by default. When on, open the shown web address on a phone on the SAME network, paste "
                "the access token, and ask HELIX or check its status. It can ONLY ask questions and run "
                "your saved agents — it can never build, delete, change HELIX, or write files remotely.",
            ))
            self._remote_enabled = QCheckBox("Let me reach HELIX from a phone")
            form.addWidget(self._check_row(
                self._remote_enabled, "Remote access",
                "Starts a small web server on THIS PC. It's protected by a secret token (shown below when "
                "on) and is limited to asking questions and running saved agents — the same read-only "
                "powers a background agent has. It never accepts builds, deletes, self-changes, or file "
                "writes. Turn it off to stop listening entirely.",
            ))
            self._remote_lan = QCheckBox("Allow other devices on my network (not just this PC)")
            form.addWidget(self._check_row(
                self._remote_lan, "Network access",
                "OFF: only this PC can reach it (127.0.0.1). ON: any device on your local network (your "
                "phone on the same Wi-Fi) can, using the address below. It is still token-protected. This "
                "does NOT expose HELIX to the internet — for access away from home, use a VPN. Never "
                "forward this port on your router.",
            ))
            self._remote_info = QLabel("")
            self._remote_info.setWordWrap(True)
            self._remote_info.setTextFormat(Qt.TextFormat.PlainText)
            self._remote_info.setStyleSheet(f"color:{MUTED};font-size:12px;")
            form.addWidget(self._remote_info)

        # ── Audio devices — which mic HELIX hears you on, and testing your speakers/earphones ──
        if _AUDIO:
            form.addSpacing(4)
            form.addWidget(self._section(
                "Audio devices",
                "Pick the microphone HELIX listens through, and test that your speakers or earphones work.",
            ))
            form.addWidget(self._audio_devices_widget())

        # ── Appearance & voice ──
        form.addSpacing(4)
        form.addWidget(self._section("Appearance & voice"))
        self._detail = QComboBox()
        self._detail.addItem("Balanced — faster, lighter", "balanced")
        self._detail.addItem("High — native poly + detailed textures", "high")
        form.addWidget(self._labeled(
            "Hologram detail", "Hologram detail",
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

    # ----- the Connections manager (review-only; values are pasted via the connect panel) -----
    def _connections_manager(self) -> QWidget:
        """One row per connectable service: a connected dot, the label, and Remove for a connected
        one. No key is ever shown or edited here — asking HELIX to connect opens the panel."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)
        for sid, (label, _store, _fields) in CONNECTABLE.items():
            row = QHBoxLayout()
            row.setSpacing(8)
            dot = QLabel()
            dot.setFixedWidth(16)
            name = QLabel(label)
            remove = QPushButton("Remove")
            remove.setStyleSheet("padding:4px 10px;font-size:12px;")
            remove.setToolTip(f"Forget the saved {label} key(s) on this machine")
            remove.clicked.connect(lambda _=False, s=sid: self._remove_connection(s))
            row.addWidget(dot)
            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(remove)
            lay.addLayout(row)
            self._conn_rows.append((sid, dot, remove))
        return box

    def _service_connected(self, service_id: str) -> bool:
        """Connected = EVERY credential the service needs has a value in its store."""
        _label, store, fields = CONNECTABLE[service_id]
        if store == "secrets":
            if self._connections is None:
                return False
            return all((self._connections.value(key) or "").strip() for key, _lbl, _hint in fields)
        return all(str(self._settings.get(key) or "").strip() for key, _lbl, _hint in fields)

    def _remove_connection(self, service_id: str) -> None:
        """Clear every stored value for this service (secrets or Settings) and refresh its row."""
        _label, store, fields = CONNECTABLE[service_id]
        for key, _lbl, _hint in fields:
            if store == "secrets":
                if self._connections is not None:
                    self._connections.set_value(key, "")
            else:
                self._settings.set(key, "")
        self._refresh_connection_rows()

    def _refresh_connection_rows(self) -> None:
        for sid, dot, remove in self._conn_rows:
            connected = self._service_connected(sid)
            dot.setText("●" if connected else "○")
            dot.setStyleSheet(f"font-size:13px;color:{STATUS_DONE if connected else MUTED};")
            remove.setVisible(connected)

    def _open_memory(self) -> None:
        from helix.ui.memory_view import MemoryDialog
        MemoryDialog(self._memory, self).exec()

    @staticmethod
    def _lan_ip() -> str:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def _refresh_remote_info(self) -> None:
        if self._remote is None:
            return
        if not self._remote.enabled():
            self._remote_info.setText("Remote access is off — nothing is listening.")
            return
        host = self._lan_ip() if self._remote.lan() else "127.0.0.1"
        url = f"http://{host}:{self._remote.port()}"
        tok = self._remote.token() or "(saved on next Save)"
        scope = "on your network" if self._remote.lan() else "on this PC only"
        self._remote_info.setText(f"Open {url} ({scope}). Access token: {tok}")

    def _check_row(self, checkbox: QCheckBox, info_title: str, info_body: str) -> QWidget:
        """A checkbox with a trailing ⓘ detail button — the same pattern as the file-write toggle."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        lay.addWidget(checkbox)
        lay.addStretch(1)
        lay.addWidget(self._info_btn(info_title, info_body))
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

    def _gmail_section(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        lbl = QLabel("Gmail — read-only inbox")
        lbl.setStyleSheet("font-weight:600;")
        status = QLabel()
        status.setStyleSheet("font-size:12px;")
        head.addWidget(lbl)
        head.addWidget(status)
        head.addStretch(1)
        head.addWidget(self._info_btn(
            "Gmail (read-only inbox)",
            "Lets HELIX answer questions about your inbox — “any new email?”, “anything from the school?”. "
            "It ONLY reads (it never marks mail as read, sends, or deletes).\n\n"
            "Setup: turn on 2-Step Verification on your Google account, then create a 16-character App "
            "Password at myaccount.google.com/apppasswords and paste it here with your Gmail address.\n\n"
            "Security note: a Google App Password grants FULL mailbox access (read, send, AND delete) and "
            "can't be scoped read-only — HELIX restricts itself to reading, but the credential itself is "
            "powerful. It's stored on this machine only, never sent anywhere except Gmail, and you can "
            "revoke it anytime at myaccount.google.com/apppasswords.",
        ))
        lay.addLayout(head)
        lay.addWidget(self._gmail_addr)
        lay.addWidget(self._gmail_pw)
        self._statuses.append((status, lambda: "set" if self._gmail.configured() else ""))
        return box

    def _calendar_section(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        lbl = QLabel("Calendar — read-only")
        lbl.setStyleSheet("font-weight:600;")
        status = QLabel()
        status.setStyleSheet("font-size:12px;")
        head.addWidget(lbl)
        head.addWidget(status)
        head.addStretch(1)
        head.addWidget(self._info_btn(
            "Calendar (read-only)",
            "Lets HELIX answer “what's on today?”, “when's my next meeting?” and give agents (like a "
            "morning brief) your schedule. It ONLY reads.\n\n"
            "Setup (Google Calendar): Settings → your calendar → “Integrate calendar” → copy the "
            "“Secret address in iCal format” and paste it here. Most other calendars offer a private "
            "iCal/ICS link too.\n\n"
            "Security note: that URL is a secret — anyone holding it can read this calendar. It's stored "
            "on this machine only and never sent anywhere except the calendar itself; you can reset it "
            "from your calendar's settings anytime.",
        ))
        lay.addLayout(head)
        lay.addWidget(self._calendar_url)
        self._statuses.append((status, lambda: "set" if self._calendar.configured() else ""))
        return box

    # ----- audio devices -----
    def _audio_devices_widget(self) -> QWidget:
        """Microphone + output pickers, each with a Test button, plus a shortcut to Windows sound settings.
        HELIX's own voice always plays to the WINDOWS DEFAULT output (your earphones when they're
        connected) — the output picker here is for testing a specific device before you rely on it."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)

        # Microphone: routes hands-free + push-to-talk capture to the chosen device.
        lay.addWidget(self._device_label("Microphone (HELIX listens here)"))
        self._mic_combo = QComboBox()
        self._mic_status = QLabel("")
        self._mic_status.setStyleSheet(f"color:{MUTED};font-size:12px;")
        mic_test = QPushButton("🎤 Test mic")
        mic_test.setToolTip("Listen for ~2 seconds and show how well this mic hears you")
        mic_test.clicked.connect(self._test_mic)
        lay.addLayout(self._device_row(self._mic_combo, mic_test))
        lay.addWidget(self._mic_status)

        # Output: HELIX follows the Windows default, but you can test ANY device to confirm it plays.
        lay.addWidget(self._device_label("Sound output (test your speakers / earphones)"))
        self._out_combo = QComboBox()
        self._out_status = QLabel("")
        self._out_status.setStyleSheet(f"color:{MUTED};font-size:12px;")
        out_test = QPushButton("🔊 Test")
        out_test.setToolTip("Play a short chime through the selected device")
        out_test.clicked.connect(self._test_output)
        lay.addLayout(self._device_row(self._out_combo, out_test))
        lay.addWidget(self._out_status)

        note = QLabel(
            "HELIX speaks through your Windows default output — your earphones automatically once they're "
            "connected. Use “Test” to confirm a device works before you step away."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED};font-size:12px;")
        lay.addWidget(note)
        sound_btn = QPushButton("Open Windows sound settings")
        sound_btn.clicked.connect(self._open_sound_settings)
        srow = QHBoxLayout()
        srow.addWidget(sound_btn)
        srow.addStretch(1)
        lay.addLayout(srow)

        self._populate_audio_devices()
        return box

    @staticmethod
    def _device_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("font-weight:600;")
        return lbl

    @staticmethod
    def _device_row(combo: QComboBox, button: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(combo, stretch=1)
        row.addWidget(button)
        return row

    def _populate_audio_devices(self) -> None:
        """Fill both combos from the current devices and re-select the saved choice. Called on load and
        each reload, so devices that come and go (Bluetooth earphones) refresh. A saved device that isn't
        present right now is kept as a placeholder row so saving while it's unplugged never drops it."""
        if not _AUDIO or self._mic_combo is None or self._out_combo is None:
            return
        saved_in = (self._settings.get(AUDIO_INPUT_SETTING, "") or "")
        saved_out = (self._settings.get(AUDIO_OUTPUT_SETTING, "") or "")
        try:
            inputs = QMediaDevices.audioInputs()
        except Exception:
            inputs = []
        try:
            outputs = QMediaDevices.audioOutputs()
        except Exception:
            outputs = []
        for combo, saved, devices, default_label in (
            (self._mic_combo, saved_in, inputs, "System default"),
            (self._out_combo, saved_out, outputs, "System default (HELIX uses this)"),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(default_label, "")
            for dev in devices:
                combo.addItem(dev.description(), device_id_str(dev))
            if saved and combo.findData(saved) < 0:
                combo.addItem("Selected device — not connected", saved)
            idx = combo.findData(saved)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    @staticmethod
    def _input_device(data: str):
        if data:
            for dev in QMediaDevices.audioInputs():
                if device_id_str(dev) == data:
                    return dev
        return QMediaDevices.defaultAudioInput()

    @staticmethod
    def _output_device(data: str):
        if data:
            for dev in QMediaDevices.audioOutputs():
                if device_id_str(dev) == data:
                    return dev
        return QMediaDevices.defaultAudioOutput()

    def _test_output(self) -> None:
        if not _AUDIO or self._out_combo is None:
            return
        dev = self._output_device(self._out_combo.currentData() or "")
        if dev is None or dev.isNull():
            self._out_status.setText("No output device available.")
            return
        fmt = QAudioFormat()
        fmt.setSampleRate(44100)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        try:
            self._test_buf = QBuffer(self)
            self._test_buf.setData(QByteArray(_chime_pcm(44100, 1)))
            self._test_buf.open(QIODevice.OpenModeFlag.ReadOnly)
            self._test_sink = QAudioSink(dev, fmt, self)
            self._test_sink.stateChanged.connect(self._on_sink_state)
            self._test_sink.start(self._test_buf)
            self._out_status.setText("Playing a test chime…")
        except Exception:
            self._out_status.setText("Couldn't play through this device.")

    def _on_sink_state(self, state) -> None:
        if state in (QAudio.State.IdleState, QAudio.State.StoppedState):
            try:
                if self._test_sink is not None:
                    self._test_sink.stop()
            except Exception:
                pass
            self._out_status.setText("Done. Heard the chime? Then this device works.")

    def _test_mic(self) -> None:
        if not _AUDIO or self._mic_combo is None:
            return
        dev = self._input_device(self._mic_combo.currentData() or "")
        if dev is None or dev.isNull():
            self._mic_status.setText("No microphone available.")
            return
        fmt = QAudioFormat()
        fmt.setSampleRate(16000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        try:
            self._mic_src = QAudioSource(dev, fmt, self)
            self._mic_io = self._mic_src.start()
        except Exception:
            self._mic_status.setText("Couldn't open this microphone.")
            return
        if self._mic_io is None:
            self._mic_src = None
            self._mic_status.setText("Couldn't open this microphone.")
            return
        self._mic_peak = 0.0
        self._mic_io.readyRead.connect(self._on_mic_ready)
        self._mic_status.setText("Listening… say something.")
        QTimer.singleShot(2000, self._finish_mic_test)

    def _on_mic_ready(self) -> None:
        if self._mic_io is None:
            return
        chunk = bytes(self._mic_io.readAll())
        if chunk:
            self._mic_peak = max(self._mic_peak, _peak_rms(chunk))

    def _finish_mic_test(self) -> None:
        try:
            if self._mic_src is not None:
                self._mic_src.stop()
        except Exception:
            pass
        self._mic_src = None
        self._mic_io = None
        peak = self._mic_peak
        if peak >= 250:
            self._mic_status.setText(f"Heard you clearly ✓  (level {int(min(100, peak / 80))}%)")
        elif peak >= 60:
            self._mic_status.setText("Picked up some sound — try speaking up, or move the mic closer.")
        else:
            self._mic_status.setText("No sound detected. Check the mic is connected and not muted.")

    @staticmethod
    def _open_sound_settings() -> None:
        QDesktopServices.openUrl(QUrl("ms-settings:sound"))

    def _refresh_statuses(self) -> None:
        for label, getter in self._statuses:
            try:
                has = bool((getter() or "").strip())
            except Exception:  # noqa: BLE001
                has = False
            label.setText("● Set" if has else "○ Not set")
            label.setStyleSheet(f"font-size:12px;color:{STATUS_DONE if has else MUTED};")

    def _refresh_brain_status(self) -> None:
        """Show which brain the next turn will use, from SAVED settings + real capability. The
        subscription is preferred but only active when a token is saved AND the local Claude Code app
        is reachable; otherwise HELIX runs on the API key. Honest about the in-between states."""
        amber = "#e0a13f"
        token = (self._settings.get("claude_code_oauth_token") or "").strip()
        key = (self._settings.get("claude_api_key") or "").strip()
        sub_live = self._subscription is not None and self._subscription.active()
        if sub_live:
            text = "● Running on your Claude subscription — off the API meter."
            color, tip = STATUS_DONE, ("Conversation, watchers, and builds draw on your Claude plan, "
                                       "the same usage pool as Claude Desktop. No restart needed.")
        elif key:
            text = "● Running on the API meter (Console billing)."
            color = amber
            tip = ("A subscription token is saved, but the Claude Code app isn't reachable, so HELIX "
                   "is using the API key. Make sure the Claude desktop app is installed."
                   if token else
                   "Add a subscription token above to switch to your Claude plan and stop metered billing.")
        elif token:
            text = "○ Token saved, but the Claude Code app isn't reachable — no billing path is active."
            color, tip = amber, ("HELIX drives the Claude desktop app's local engine; install/open "
                                 "Claude desktop, or add an API key as a fallback.")
        else:
            text = "○ Not connected — add a subscription token (recommended) or an API key above."
            color, tip = MUTED, ""
        self._brain_status.setText(text)
        self._brain_status.setStyleSheet(f"font-size:12px;font-weight:600;color:{color};")
        self._brain_status.setToolTip(tip)

    # ----- load / save -----
    def reload(self) -> None:
        self._oauth_token.setText(self._settings.get("claude_code_oauth_token", "") or "")
        self._key.setText(self._settings.get("claude_api_key", "") or "")
        self._evolve.setChecked(bool(self._settings.get("evolve_enabled", True)))  # missing = ON
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
        if self._gmail is not None:
            self._gmail_addr.setText(self._gmail.address())
            self._gmail_pw.setText(self._gmail.app_password())
        if self._calendar is not None:
            self._calendar_url.setText(self._calendar.url())
        self._file_write.setChecked(bool(self._settings.get(WRITE_ACCESS_KEY)))
        self._wake_word.setText(self._settings.get("wake_word", "") or "")
        nmode = (self._settings.get("narration_mode") or "off").lower()
        nidx = self._narration.findData(nmode)
        self._narration.setCurrentIndex(nidx if nidx >= 0 else 0)
        self._proactive.setChecked(bool(self._settings.get("proactive_speech", False)))
        self._trust_voice.setChecked(bool(self._settings.get("trust_household_voice", False)))
        if self._remote is not None:
            self._remote_enabled.setChecked(self._remote.enabled())
            self._remote_lan.setChecked(self._remote.lan())
            self._refresh_remote_info()
        self._populate_audio_devices()  # refresh the device lists (earphones may have come/gone)
        self._refresh_connection_rows()  # a service connected mid-conversation shows up on reopen
        self._refresh_statuses()
        self._refresh_brain_status()

    def _save(self) -> None:
        self._settings.set("claude_code_oauth_token", self._oauth_token.text().strip())
        self._settings.set("claude_api_key", self._key.text().strip())
        self._settings.set("evolve_enabled", self._evolve.isChecked())
        self._settings.set("model_detail", self._detail.currentData())
        self._settings.set("tts_voice", self._voice.currentData())
        self._settings.set("tts_rate", round(self._speed.value() / 10, 1))
        if self._gmail is not None:
            self._gmail.set_credentials(self._gmail_addr.text(), self._gmail_pw.text())
        if self._calendar is not None:
            self._calendar.set_url(self._calendar_url.text())
        self._settings.set(WRITE_ACCESS_KEY, self._file_write.isChecked())
        self._settings.set("wake_word", self._wake_word.text().strip())
        self._settings.set("narration_mode", self._narration.currentData())
        self._settings.set("proactive_speech", self._proactive.isChecked())
        self._settings.set("trust_household_voice", self._trust_voice.isChecked())
        if self._remote is not None:
            from helix.services.remote import ENABLED_KEY, LAN_KEY
            self._settings.set(ENABLED_KEY, self._remote_enabled.isChecked())
            self._settings.set(LAN_KEY, self._remote_lan.isChecked())
            if self._remote_enabled.isChecked():
                self._remote.ensure_token()  # mint the access token so it's shown below
            self._refresh_remote_info()
        if _AUDIO and self._mic_combo is not None and self._out_combo is not None:
            self._settings.set(AUDIO_INPUT_SETTING, self._mic_combo.currentData() or "")
            self._settings.set(AUDIO_OUTPUT_SETTING, self._out_combo.currentData() or "")
        self._refresh_connection_rows()
        self._refresh_statuses()
        self._refresh_brain_status()  # a just-saved token flips this to "on your subscription" at once
        self._status.setText("Saved.")
        self.saved.emit()
