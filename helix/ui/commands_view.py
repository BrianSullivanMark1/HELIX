"""CommandsDialog — a pop-up reference of what you can say (and click) to HELIX.

Part of the Forge's immutable shell (helix/ui/). It documents the real keywords the voice/text layer
recognizes plus the manual controls, each with a one-sentence action — so a voice-first app is never a
guessing game. Pure presentation: a static, grouped list rendered in the HUD look.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from helix.ui.theme import CYAN, LINE, MUTED, TEXT

# (section title, [(keyword/phrase, one-sentence action)]). Keep each action to ONE plain sentence.
COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Wake & talk", (
        ("“{WAKE} …”", "Say the wake word, then your request (change the word in Settings)."),
        ("Tap the orb", "Start talking hands-free — or stop HELIX when it's busy."),
        ("🎤 Hold to Talk", "Push-to-talk: hold the button, speak, release."),
    )),
    ("Build things — just describe them", (
        ("“build a tip calculator”", "HELIX confirms, then builds an app into your menu."),
        ("“show me a 3D heart”", "Projects an interactive hologram you can orbit and explore."),
        ("“make a protocol that renames my downloads”", "Builds a script you run on demand."),
        ("“remember the wifi password”", "Saves a note to your searchable vault."),
        ("“save a morning-brief agent”", "Saves a standing goal you can run any time."),
    )),
    ("Change what you've made", (
        ("“make it taller” · “make the streak monthly”", "Iterates the build you're discussing, in place."),
        ("“rename the tip calculator to Gratuity”", "Renames any app, protocol, hologram, or agent."),
        ("“run my morning brief”", "Runs a saved agent (or “run the cleanup protocol”)."),
        ("“delete the tip calculator”", "Removes it — after you confirm with one click."),
    )),
    ("Stop a build or a reply", (
        ("■ Stop button", "Halts whatever HELIX is doing right now."),
        ("Esc, or tap the orb", "The same — stop the current build or reply."),
        ("“stop” · “stop build” · “cancel the build” · “abort”", "By voice when HELIX is idle; while it's "
         "building or speaking the mic is off — so a baby or the TV can't cancel your work — use Stop."),
    )),
    ("Sleep the mic — your build keeps running", (
        ("“sleep” · “go to sleep” · “stop listening”", "Rests the mic so HELIX stops hearing you."),
        ("“wake” · “{WAKE}” · “start listening”", "Wakes the mic and starts listening to you again."),
        ("😴 Sleep / ▶ Wake button", "The same rest/wake, by hand, beside the text box."),
    )),
    ("Voice & session", (
        ("🔊 Voice button", "Turn hands-free listening on or off — never stops a build."),
        ("“goodbye” · “that's all”", "Ends the voice session, back to wake-word only."),
        ("📎 Attach", "Add files or a folder as context for your next message."),
    )),
    ("Who's speaking — voice recognition", (
        ("“Hey, I am Brian”", "Registers your voice: a short, friendly calibration chat, then HELIX "
                              "knows you. Anyone in the house can register their own voice."),
        ("“recalibrate my voice”", "Refreshes your voice profile any time with a quick guided chat."),
        ("Unrecognized voices", "Once someone is registered, spoken commands from voices HELIX doesn't "
                                "know are never acted on — it only offers to register them."),
    )),
    ("Improve HELIX itself", (
        ("“improve HELIX: …”", "Drafts a change to HELIX's own code for you to approve."),
        ("“apply it” / “discard it”", "Ships or drops the drafted self-change."),
    )),
)


class CommandsDialog(QDialog):
    """A scrollable, HUD-styled reference of HELIX's keywords and controls."""

    def __init__(self, parent=None, wake_word: str = "HELIX") -> None:
        super().__init__(parent)
        self._wake = (wake_word or "").strip() or "HELIX"
        self.setWindowTitle("HELIX — Commands")
        self.setMinimumSize(560, 620)
        self.setStyleSheet(
            f"QDialog{{background:#080b0f;}} QLabel{{color:{TEXT};}}"
            f"QScrollArea{{border:none;background:transparent;}}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        title = QLabel("Things you can say")
        title.setStyleSheet(f"color:{CYAN};font-size:18px;font-weight:600;letter-spacing:.5px;")
        subtitle = QLabel(
            f"Speak the wake word “{self._wake}” first when hands-free, or just type any of these."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{MUTED};")
        root.addWidget(title)
        root.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        host.setStyleSheet("background:transparent;")
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 4, 8, 4)
        col.setSpacing(14)
        for section, rows in COMMAND_GROUPS:
            col.addWidget(self._section(section, rows))
        col.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, stretch=1)

        close = QPushButton("Close")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(close)
        root.addLayout(brow)

    def _section(self, title: str, rows: tuple[tuple[str, str], ...]) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        card.setStyleSheet(
            f"QFrame#Card{{background:rgba(13,20,27,0.6);border:1px solid {LINE};border-radius:12px;}}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setStyleSheet(f"color:{CYAN};font-weight:600;font-size:13px;letter-spacing:.4px;")
        lay.addWidget(head)
        for keys, action in rows:
            row = QLabel(f"{keys.replace('{WAKE}', self._wake)}  —  {action}")
            row.setWordWrap(True)
            row.setTextFormat(Qt.TextFormat.PlainText)
            row.setStyleSheet(f"color:{TEXT};")
            lay.addWidget(row)
        return card
