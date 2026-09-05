"""Web bootstrap — build the container, start the localhost server, open the face.

The web counterpart of bootstrap.py: same container, same recovery, same teardown duties — but the
shell is a React page served over 127.0.0.1 and the window is pywebview (Edge WebView2) instead of
Qt. No PyQt6 import happens anywhere on this path; voice runs in-process through the Qt-free
WebVoice loop (sounddevice + the shared grammar), so the launcher's STT pre-warm applies unchanged.

This module is under helix/app/ and therefore PROTECTED — the self-coder may never edit the wiring.
"""
from __future__ import annotations

import os
import threading
import webbrowser
from pathlib import Path

from helix.api.server import DEFAULT_PORT, PORT_SETTING, EventHub, build_app
from helix.api.shell import ShellSession
from helix.app.container import Container
from helix.logging_setup import get_logger

_LOG = get_logger("webboot")


def _web_dist(app_root: Path) -> Path | None:
    """The built SPA. In dev it's web/dist beside the repo; frozen, build.py ships it under
    helix/webui. None means 'backend only' (the Vite dev server can front it instead)."""
    for candidate in (app_root / "web" / "dist", Path(__file__).resolve().parents[1] / "webui"):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def run_web(open_mode: str = "window") -> int:
    container = Container()
    try:
        container.forge.recover_interrupted()
    except Exception:  # noqa: BLE001
        _LOG.exception("interrupted-build recovery failed")
    try:
        container.selfdev.recover_interrupted()
    except Exception:  # noqa: BLE001
        _LOG.exception("interrupted self-change recovery failed")

    hub = EventHub()

    # Voice: optional and additive, exactly as in the Qt shell. Wired before the shell so the
    # shell's constructor can read its state; callbacks land as WS events.
    voice = None
    try:
        from helix.api.voice_loop import WebVoice

        voice = WebVoice(
            container.settings, container.speech_in, container.speech_out,
            voice_id=container.voice_id, reflexes=container.reflexes,
            on_state=lambda s: hub.push({"t": "orb", "state": s}),
            on_level=lambda v: hub.push({"t": "level", "v": round(v, 3)}),
            on_bands=lambda b: hub.push({"t": "bands", "v": [round(x, 3) for x in b]}),
        )
    except Exception:  # noqa: BLE001 — a broken audio stack still leaves a clean text app
        _LOG.warning("voice loop unavailable — text-only", exc_info=True)
        voice = None

    shell = ShellSession(container, hub.push, voice=voice)
    if voice is not None:
        voice.on_recognized = shell.on_voice_recognized
        voice.on_stop = shell.stop
        voice.on_identity = shell.on_voice_identity
        voice.on_muted = lambda _m: shell.push({"t": "voice", **shell.voice_state()})

    app = build_app(container, shell, hub, _web_dist(container.paths.root))

    def graceful_quit() -> None:
        """Settings → Quit HELIX: the same teardown the finally block runs, then end the process
        (uvicorn's thread would otherwise keep it alive forever in browser mode)."""
        _LOG.info("quit requested from the face")
        try:
            shell.shutdown()
        except Exception:  # noqa: BLE001
            pass
        for fn in (container.tasks.stop_all, container.build_queue.shutdown,
                   container.selfdev_lane.shutdown, container.subscription.shutdown,
                   container.store.close):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        os._exit(0)

    app.state.quit = graceful_quit
    port = int(container.settings.get(PORT_SETTING) or DEFAULT_PORT)
    token = app.state.token
    url = f"http://127.0.0.1:{port}/?t={token}"

    import uvicorn

    # log_config=None: uvicorn must not install its own logging (its default formatter probes
    # sys.stdout.isatty() — fatal in a windowed frozen app — and would restyle root logging besides).
    # Its records flow into HELIX's rotating-file logging like everyone else's.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                            log_config=None, access_log=False)
    server = uvicorn.Server(config)

    started = threading.Event()

    def serve() -> None:
        # Grab the loop for the hub as soon as uvicorn makes it.
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        hub.attach_loop(loop)
        started.set()
        loop.run_until_complete(server.serve())

    server_thread = threading.Thread(target=serve, daemon=True, name="helix-web")
    server_thread.start()
    started.wait(5.0)
    _LOG.info("web shell listening at http://127.0.0.1:%s", port)

    exit_code = 0
    try:
        if open_mode == "window":
            try:
                import webview  # pywebview: an Edge WebView2 window — HELIX as its own app

                webview.create_window(
                    "HELIX", url, width=1180, height=820, background_color="#080b0f",
                )
                webview.start()  # blocks until the window closes
            except Exception:  # noqa: BLE001 — no WebView2? the browser is the window
                _LOG.warning("pywebview window unavailable — opening the browser", exc_info=True)
                webbrowser.open(url)
                server_thread.join()
        elif open_mode == "browser":
            webbrowser.open(url)
            server_thread.join()
        else:  # "none": headless serve (dev — Vite fronts it)
            print(f"HELIX web backend: {url}")
            server_thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        _LOG.info("web shell shutting down")
        try:
            shell.shutdown()
        except Exception:  # noqa: BLE001
            pass
        for label, fn in (
            ("app servers", lambda: container.tasks.stop_all()),  # no orphans holding build folders
            ("build queue", lambda: container.build_queue.shutdown()),
            ("selfdev lane", lambda: container.selfdev_lane.shutdown()),
            ("subscription", lambda: container.subscription.shutdown()),
            ("store", lambda: container.store.close()),
        ):
            try:
                fn()
            except Exception:  # noqa: BLE001
                _LOG.warning("teardown: %s failed", label, exc_info=True)
        server.should_exit = True
    return exit_code
