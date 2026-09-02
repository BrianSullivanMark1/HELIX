"""The composition root actually composes.

Nothing else in the suite constructs `Container`. Every other test builds the one or two services it
needs by hand, so a wiring break — a renamed service, a constructor argument that drifted, an adapter
that moved — is invisible to the whole suite and only shows up when Brian launches the app. This builds
the real thing against a throwaway data dir.

It also guards two properties that are otherwise only provable by launching: that wiring stays cheap
(the heavy import stacks must not be dragged in while composing), and that the "which Claude rail is
live" diagnostic reports the actual missing piece rather than blaming a credential.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import helix.config as config  # noqa: E402

# Every service the UI and the tool registry reach for off the container.
EXPECTED_SERVICES = (
    "paths", "settings", "store", "repo", "clock", "bus", "subscription", "chat", "coder",
    "growth_coder", "builds", "secrets", "model_baker", "forge", "build_queue", "selfdev",
    "selfdev_lane", "connections", "knowledge", "files", "user_memory", "tools", "profile",
    "lessons", "evolve", "conversation", "agents", "scheduler", "workflows", "speech_in",
    "speech_out", "voice_id", "cad",
)


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def container(_app, tmp_path, monkeypatch):
    """The real Container, pointed at a temp data dir so the user's own data is never touched."""
    root = config.AppPaths.resolve().root      # the repo itself — read-only for our purposes
    monkeypatch.setattr(
        config.AppPaths, "resolve",
        staticmethod(lambda: config.AppPaths(root=root, data=tmp_path)),
    )
    from helix.app.container import Container

    return Container()


def test_container_composes_every_service(container):
    missing = [name for name in EXPECTED_SERVICES if not hasattr(container, name)]
    assert not missing, f"container no longer wires: {missing}"


def test_container_exposes_a_tool_surface(container):
    specs = container.tools.specs()
    assert len(specs) > 20, f"only {len(specs)} tools registered — the registry lost most of its surface"
    names = {s.name for s in specs}
    assert len(names) == len(specs), "duplicate tool names would make dispatch ambiguous"


def test_composing_does_not_drag_in_the_heavy_stacks(tmp_path):
    """The deferral in tests/test_startup_cost.py is about IMPORTING the container module; this is about
    CONSTRUCTING it. Moving an import is useless if wiring still builds the object that needs it — which
    is why the hologram baker is constructed lazily behind a proxy.

    Runs in a FRESH interpreter on purpose: other tests in a full run may import anthropic (or, in an
    older tree, trimesh) quite legitimately, so asserting on this process's sys.modules would pass alone
    and fail in a full run — an order-dependent test, which is worse than none."""
    import subprocess

    script = (
        "import os, sys\n"
        "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
        "from pathlib import Path\n"
        "import helix.config as config\n"
        "root = config.AppPaths.resolve().root\n"
        f"data = Path(r'{tmp_path}')\n"
        "config.AppPaths.resolve = staticmethod(lambda: config.AppPaths(root=root, data=data))\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "QApplication.instance() or QApplication([])\n"
        "from helix.app.container import Container\n"
        "Container()\n"
        "print(','.join(m for m in ('trimesh', 'networkx', 'anthropic') if m in sys.modules) or 'clean')\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"constructing the container failed:\n{proc.stderr[-3000:]}"
    verdict = proc.stdout.strip().splitlines()[-1]
    assert verdict == "clean", (
        f"constructing the container pulled in {verdict} — a lazy seam (the baker proxy, or the "
        f"deferred anthropic import) is no longer lazy"
    )


def test_rail_diagnostic_names_the_missing_piece_not_the_credential(container):
    """A fresh data dir has no token and no API key. The diagnostic must say which, in words, because
    the failure it replaced told users to check a token that was often perfectly good."""
    assert container.subscription.active(allow_probe=False) is False
    reason = container.subscription.why_inactive(allow_probe=False)
    assert reason, "why_inactive() went quiet about an unusable rail"
    assert "setup-token" in reason, f"expected the missing token to be named, got: {reason}"


# ---------------------------------------------------------------------------------------------
# The other half of wiring: the SHELL. Several fixes live entirely in a service or a view and are
# reached by exactly one line in helix/ui/main_window.py — a forwarded flag, an argument passed to a
# dialog, a teardown step. Those halves each have their own pin where they live, and every one of them
# stayed green while the bug was still live in the running app, because nothing asserted that the shell
# actually CALLS them. These do. They drive the bridge methods unbound against a recording stand-in, so
# there is no window, no event loop and no thread timing involved — just the call the shell makes.
# ---------------------------------------------------------------------------------------------

class _RecordingConsole:
    """Stands in for ConsoleView: remembers exactly what the shell handed it."""

    def __init__(self) -> None:
        self.progress: list[tuple] = []
        self.finished: list[dict] = []

    def on_self_change_progress(self, line: str, unattended: bool = False) -> None:
        self.progress.append((line, unattended))

    def on_self_change_finished(self, ok, summary, branch, error, stopped, unattended: bool = False):
        self.finished.append({"ok": ok, "summary": summary, "branch": branch, "error": error,
                              "stopped": stopped, "unattended": unattended})


def test_the_shell_forwards_unattended_to_the_console(monkeypatch):
    """The 3 AM silence is DECIDED in console_view, but it is only reachable if the shell passes the
    fact along. This is the line that was missing: the console's own pin passed while the overnight
    Evolve pass still narrated every step aloud, because the bridge dropped the flag on the floor."""
    from types import SimpleNamespace

    from helix.ui.main_window import HelixMainWindow

    console = _RecordingConsole()
    shell = SimpleNamespace(console=console)

    HelixMainWindow._on_self_change_progress(
        shell, SimpleNamespace(line="reading the file", unattended=True))
    assert console.progress == [("reading the file", True)], (
        "the shell dropped `unattended` on the way to the console — the overnight pass will talk"
    )

    HelixMainWindow._on_self_change_finished(shell, SimpleNamespace(
        ok=True, summary="a quieter orb", branch="evolve/quiet", error=None, stopped=False,
        unattended=True))
    assert console.finished[-1]["unattended"] is True, (
        "the finished bridge dropped `unattended` — 3 AM would hear the whole announcement"
    )


def test_an_attended_self_change_still_reaches_the_console_as_spoken():
    """The other side of the same line: a change the user ASKED for must stay narrated. A bridge that
    hardcoded True would pass the test above and silence HELIX for everyone."""
    from types import SimpleNamespace

    from helix.ui.main_window import HelixMainWindow

    console = _RecordingConsole()
    shell = SimpleNamespace(console=console)

    HelixMainWindow._on_self_change_progress(shell, SimpleNamespace(line="writing the patch"))
    HelixMainWindow._on_self_change_finished(shell, SimpleNamespace(
        ok=False, summary="", branch="", error="no room on disk", stopped=False))

    assert console.progress == [("writing the patch", False)]
    assert console.finished[-1]["unattended"] is False
    assert console.finished[-1]["error"] == "no room on disk", "the shell garbled the finished payload"


def test_the_shell_hands_the_sleep_holder_to_the_console():
    """go_to_sleep parks its worker on a SleepRequest holder waiting to hear whether the ears really
    closed. Every other half of that chain landed and was pinned green — the domain holder, the tool
    that publishes and parks, the console that claims and settles it — while this one bridge threw the
    event away, so the turn burned its claim timeout and then reported that nothing was listening about
    a mic it had just muted. The suite could not see it because nothing drove the bridge."""
    from types import SimpleNamespace

    from helix.domain.events import SleepRequest, SleepRequested
    from helix.ui.main_window import HelixMainWindow

    seen: list = []
    shell = SimpleNamespace(console=SimpleNamespace(sleep_voice=seen.append))

    req = SleepRequest()
    HelixMainWindow._on_sleep_requested(shell, SleepRequested(request=req))
    assert seen == [req], "the shell dropped the sleep holder — the turn will time out and misreport"

    # A bare event from any other publisher must still rest the mic, not crash the shell.
    HelixMainWindow._on_sleep_requested(shell, SleepRequested())
    assert seen[-1] is None


def test_the_connect_panel_is_told_which_keys_helix_already_manages(monkeypatch):
    """ConnectionsDialog grew an optional `is_managed` lookup so a build asking for the Claude key can
    say "already connected in HELIX — leave it as is" instead of demanding a credential the user has no
    copy of. The shell built the dialog positionally and never passed it, so that affordance was dead in
    every real run while the dialog's own pin stayed green."""
    from types import SimpleNamespace

    from helix.ui import main_window as mw

    seen: dict = {}

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs

        def exec(self):
            return False  # the user closed it; nothing to refresh

    monkeypatch.setattr(mw, "ConnectionsDialog", _FakeDialog)

    connections = SimpleNamespace(
        declared=lambda slug: [object()],       # this build does declare a key
        value=lambda key: "",
        set_value=lambda key, value: None,
        is_managed=lambda key: key == "ANTHROPIC_API_KEY",
    )
    shell = SimpleNamespace(_c=SimpleNamespace(connections=connections),
                            _refresh_build_ui=lambda: None)

    mw.HelixMainWindow._on_connect_build(shell, "weather", "Weather")

    passed = list(seen["args"]) + list(seen["kwargs"].values())
    assert connections.is_managed in passed, (
        "the Connect panel was built without connections.is_managed — a frozen build will ask the "
        "user to paste the Claude key HELIX already holds"
    )


def test_teardown_waits_on_the_vault_views_threads():
    """KnowledgeView runs its ingests and searches on background QThreads and has a shutdown() that
    joins them, exactly like the console and the launcher. It was called nowhere, so quitting HELIX
    mid-ingest destroyed a running QThread. The reap must include it."""
    from types import SimpleNamespace

    from helix.ui.main_window import HelixMainWindow

    called: list[str] = []

    def _step(name):
        return lambda *a, **k: called.append(name)

    shell = SimpleNamespace(
        _remote=SimpleNamespace(stop=_step("remote")),
        _close_camera_panel=_step("camera"),
        _shutdown_heartbeat=_step("heartbeat"),
        _stop_app_servers=_step("servers"),
        _viewer=None,
        console=SimpleNamespace(shutdown=_step("console")),
        launcher=SimpleNamespace(shutdown=_step("launcher")),
        _knowledge_view=SimpleNamespace(shutdown=_step("vault")),
        _c=SimpleNamespace(
            build_queue=SimpleNamespace(shutdown=_step("queue")),
            selfdev_lane=SimpleNamespace(shutdown=_step("selfdev")),
            subscription=SimpleNamespace(shutdown=_step("subscription")),
            store=SimpleNamespace(close=_step("store")),
        ),
    )

    HelixMainWindow.teardown(shell)

    assert "vault" in called, (
        "teardown never joined the Vault view's threads — closing during an ingest destroys a "
        "running QThread"
    )
    # The Vault join has to happen while the store is still open (a worker may be mid-read), so it
    # belongs before the store closes — and after the queues, like every other view.
    assert called.index("vault") < called.index("store")


# ---------------------------------------------------------------------------------------------
# The hologram engine and its critic. A hologram is an OpenSCAD program compiled through the CadEngine
# port; the container constructs that adapter ONCE and hands the same instance to the baker (which
# compiles with it) and to the tool registry (which pre-flights build_3d_model with it and offers the
# install). Two instances would let "installed" and "missing" disagree between the two; a baker built
# with cad=None would silently write the install page for every design even with OpenSCAD present —
# and the baker's own tests, which use fakes, would never notice.
# ---------------------------------------------------------------------------------------------

def test_the_baker_and_the_tool_registry_share_one_cad_engine(container):
    from helix.adapters.build123d_cad import Build123dCad
    from helix.services.model_baker import ModelBaker

    assert isinstance(container.cad, Build123dCad), "the container must construct the OpenSCAD adapter"
    assert container.tools._cad is container.cad, (
        "ToolRegistry was built without cad= — the pre-flight and install_openscad are never offered"
    )
    real = container.model_baker._get()  # the lazy proxy's real baker, built now
    assert isinstance(real, ModelBaker)
    assert real._cad is container.cad, "ModelBaker was built without the same engine the registry has"
    assert real._critic is not None, "the vision critic was not wired into the baker"
    # The viewer's three.js is the vendored build under helix/ui/assets, handed over as a plain Path.
    assert real._three_js is not None and real._three_js.name == "three.min.js" and real._three_js.is_file()


def test_the_lazy_baker_proxy_forwards_every_public_method_of_the_real_baker(container):
    """The Forge never holds the real ModelBaker in production — it holds _LazyBaker, the startup-cost
    seam — so the proxy's surface IS the contract. When prepare() was added to the baker and the Forge
    began calling it before every hologram's coder run, the proxy still forwarded only the older three
    methods: every hologram build in the running app would have died with an AttributeError while the
    suite, which hands the Forge a real baker, stayed green. Assert the surfaces match, so the next
    baker method cannot ship half-wired."""
    import inspect

    from helix.app.container import _LazyBaker
    from helix.services.model_baker import ModelBaker

    public = {
        name for name, member in inspect.getmembers(ModelBaker, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public >= {"prepare", "check", "bake", "engine_missing"}, "the baker lost part of its surface"
    missing = sorted(name for name in public if not callable(getattr(_LazyBaker, name, None)))
    assert not missing, (
        f"_LazyBaker does not forward {missing} — the Forge will AttributeError on the first hologram"
    )

    # And the forwarders really reach the real object with the argument intact, not just exist.
    class _Seen:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            return lambda *a, **k: self.calls.append((name, a, k))

    seen = _Seen()
    proxy = _LazyBaker(lambda: seen)
    proxy.prepare("ws-1")
    proxy.check("ws-2")
    proxy.bake("ws-3")
    proxy.engine_missing()
    assert seen.calls == [("prepare", ("ws-1",), {}), ("check", ("ws-2",), {}),
                          ("bake", ("ws-3",), {}), ("engine_missing", (), {})]


def _png(tmp_path):
    from PIL import Image as PILImage

    path = tmp_path / "preview.png"
    PILImage.new("RGB", (8, 8), (16, 22, 28)).save(path)
    return path


class _FakeChat:
    """Records the one call the critic makes and answers with a scripted reply."""

    def __init__(self, reply_text: str, raises: bool = False) -> None:
        from helix.ports.llm import Reply, Text

        self._reply = Reply(blocks=(Text(reply_text),))
        self._raises = raises
        self.calls: list[dict] = []

    def chat(self, turns, *, system=None, tools=None):
        self.calls.append({"turns": turns, "system": system, "tools": tools})
        if self._raises:
            raise RuntimeError("network down")
        return self._reply


def test_the_critic_says_nothing_on_ok_and_hands_back_the_problem_sentence(tmp_path):
    from helix.app.container import make_hologram_critic
    from helix.ports.llm import Image

    png = _png(tmp_path)
    ok_chat = _FakeChat("OK")
    assert make_hologram_critic(ok_chat, lambda: True)(png, "Design: bracket") is None
    # The picture actually travelled: an Image block in the user turn, the brief as text, the critic's
    # instruction as the system prompt. Without the Image the model would judge a preview it cannot see.
    turn = ok_chat.calls[0]["turns"][0]
    assert any(isinstance(b, Image) for b in turn.blocks), "the preview PNG was not sent as an Image"
    assert "Design: bracket" in "".join(getattr(b, "text", "") for b in turn.blocks)
    assert "exactly OK" in ok_chat.calls[0]["system"]
    assert ok_chat.calls[0]["tools"] is None, "the critic must be a plain chat — no tools"

    problem = "The second mounting hole does not go through the plate."
    verdict = make_hologram_critic(_FakeChat(problem + " Otherwise fine."), lambda: True)(png, "brief")
    assert verdict == problem, "the first sentence of a problem verdict is the critique"
    # Lenient about HOW the model says OK — a false problem would spend the only repair pass on taste.
    assert make_hologram_critic(_FakeChat("OK — it matches the brief."), lambda: True)(png, "b") is None


def test_the_critic_abstains_without_a_rail_and_on_any_failure(tmp_path):
    from helix.app.container import make_hologram_critic

    png = _png(tmp_path)
    railless = _FakeChat("The plate is missing.")
    assert make_hologram_critic(railless, lambda: False)(png, "brief") is None
    assert railless.calls == [], "with no usable rail the critic must not call the model at all"
    # A critic outage must never fail a build: a raising chat and an unreadable picture both read as
    # "looks right" and the build goes on.
    assert make_hologram_critic(_FakeChat("x", raises=True), lambda: True)(png, "brief") is None
    assert make_hologram_critic(_FakeChat("The plate is missing."), lambda: True)(
        tmp_path / "missing.png", "brief") is None


def test_the_critic_caps_a_rambling_verdict(tmp_path):
    from helix.app.container import make_hologram_critic

    long = "The bracket " + "is very wrong and " * 40 + "has no holes"
    verdict = make_hologram_critic(_FakeChat(long), lambda: True)(_png(tmp_path), "brief")
    assert verdict is not None and len(verdict) <= 200


@pytest.mark.parametrize("verdict", ["**OK**", '"OK"', "Okay", "Okay.", "OK", "ok — matches the brief."])
def test_a_dressed_up_ok_is_still_ok(verdict):
    """The lenient parse read only a LETTER run at the very start, so a model that bolded or quoted its
    verdict — "**OK**", '"OK"' — or wrote "Okay" was parsed as a PROBLEM and sent back to the repair
    prompt as "Looking at the rendered preview…: **OK**. Fix the model…": the build's only repair pass
    spent on a design the critic had just approved, the exact expensive mistake the leniency exists to
    avoid."""
    from helix.app.container import _critic_verdict

    assert _critic_verdict(verdict) is None, f"{verdict!r} was read as a problem"


def test_a_real_problem_sentence_is_still_handed_back():
    """The other side of the leniency: the OK parse must not swallow a genuine critique, and a verdict
    is clipped to its FIRST sentence so the repair prompt gets one problem, not an essay."""
    from helix.app.container import _critic_verdict

    problem = "The second mounting hole does not go through the plate."
    assert _critic_verdict(problem + " Otherwise it is fine.") == problem
    assert _critic_verdict("Okay-ish, but the plate is missing.") is None  # opens with the word okay
    assert _critic_verdict("Okra stems are floating above the base.") is not None  # not the word OK


def test_the_container_wires_the_critic_to_the_subscription_rail_with_no_api_key(container, tmp_path):
    """Brian's machine runs SUBSCRIPTION-ONLY (the API key was removed). The critic was wired to the
    API chat behind an API-key gate, so on the one machine this redesign is for every hologram compiled
    and rendered while the vision critique silently never happened. Now: a fresh data dir has NO API
    key; with a live subscription the critic must run, and the preview must reach the subscription's
    run_hermetic as an Image block — not be dropped by the text-only flatten."""
    from helix.ports.llm import Image

    assert not (container.settings.get("claude_api_key") or "").strip(), "this pin needs NO api key"
    seen: list[dict] = []

    def _fake_hermetic(prompt, names=(), **kw):
        seen.append({"prompt": prompt, **kw})
        return "**OK**"

    monkeypatch_sub = container.subscription
    monkeypatch_sub.active = lambda *a, **k: True          # the plan is live
    monkeypatch_sub.run_hermetic = _fake_hermetic          # ...and records what it was handed
    critic = container.model_baker._get()._critic
    verdict = critic(_png(tmp_path), "Design: bracket")
    assert verdict is None, f"the subscription said OK; that is not a problem: {verdict!r}"
    assert seen, "the critic never reached the subscription rail — it still abstained without a key"
    call = seen[0]
    images = call.get("images") or ()
    assert any(isinstance(b, Image) for b in images), f"the preview did not ride as an Image: {call!r}"
    assert "Design: bracket" in call["prompt"]
    assert call.get("web", False) is False, "a critic must never be able to search or fetch"


def test_the_container_critic_abstains_when_no_rail_can_serve(container, tmp_path, monkeypatch):
    """No subscription, no key: the critic must skip the look before it even loads the picture — not
    fall into a model call that raises and logs a warning on every hologram. Wired through the
    rail-preferring chat, the only thing standing between "abstain" and "try, fail, warn" is the gate."""
    import helix.app.container as container_mod

    container.subscription.active = lambda *a, **k: False
    container.subscription.run_hermetic = lambda *a, **k: pytest.fail("the subscription was called")

    def _never(*a, **k):
        pytest.fail("the critic loaded the preview with no rail to show it to")

    monkeypatch.setattr(container_mod, "load_image_block", _never)
    critic = container.model_baker._get()._critic
    assert critic(_png(tmp_path), "brief") is None
