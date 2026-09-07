"""CLI — argparse entry. Bare `helix` opens the desktop app; `helix watchdog ...` runs the tiny
crash-guard process (spawned by bootstrap — never run by hand), which must import no Qt."""
from __future__ import annotations

import argparse
from pathlib import Path


def backend_alive(port: int, token: str, *, tries: int = 3, timeout: float = 1.2) -> bool:
    """Is a HELIX backend actually answering on this port with this token? A cheap authenticated GET of
    /api/snapshot — urllib only, no Qt, no new dependency. A couple of retries ride out a momentarily
    busy server; a dead or dying one fails fast. Never raises."""
    import time
    import urllib.request

    for attempt in range(max(1, tries)):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/snapshot", headers={"X-Helix-Token": token})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — refused/timeout/reset all mean the same thing here
            pass
        if attempt + 1 < tries:
            time.sleep(0.4)
    return False


def open_running_face() -> bool:
    """HELIX is already running — the icon click means "show me HELIX", so open (another) browser
    tab on the running backend and step aside. Port + token live in settings, written by the running
    instance at its own startup.

    Returns True only when the backend PROVED it is alive first and the tab was pointed at it. The
    probe is the fix for the quit-then-click race: the singleton lock is held until the dying process
    exits, and blindly opening a tab there handed the user a connection error AND left nothing
    running. On False the caller (main.py's gate) waits for the lock instead and boots in its place."""
    try:
        import webbrowser

        from helix.adapters.json_settings import JsonSettings
        from helix.api.server import DEFAULT_PORT, PORT_SETTING, TOKEN_SETTING
        from helix.config import AppPaths

        settings = JsonSettings(AppPaths.resolve().settings_file)
        token = (settings.get(TOKEN_SETTING) or "").strip()
        port = int(settings.get(PORT_SETTING) or DEFAULT_PORT)
        if not token or not backend_alive(port, token):
            return False
        webbrowser.open(f"http://127.0.0.1:{port}/?t={token}")
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="helix", description="HELIX — a local-first desktop app-builder you talk to."
    )
    parser.add_argument(
        "command", nargs="?", default="ui", choices=["ui", "web", "qt", "watchdog", "cadworker"],
        help="what to run (default: ui — the web shell; 'qt' forces the legacy Qt shell)",
    )
    parser.add_argument("job", nargs="?", help="(cadworker) the job file, or --serve")
    parser.add_argument("--browser", action="store_true",
                        help="(web) open in the default browser (this is already the default)")
    parser.add_argument("--window", action="store_true",
                        help="(web) open in HELIX's own app window (WebView2) instead of the browser")
    parser.add_argument("--headless", action="store_true",
                        help="(web) serve only; print the URL (for the Vite dev server)")
    parser.add_argument("--pid", type=int, help="(watchdog) the HELIX process to guard")
    parser.add_argument("--data", help="(watchdog) the data directory")
    parser.add_argument("--entry", help="(watchdog) the entry script to relaunch")
    parser.add_argument("--root", help="(watchdog) the app root / working directory")
    args = parser.parse_args(argv)

    if args.command == "cadworker":  # the hologram compile worker — no Qt, no singleton, no STT
        from helix.cad import runner

        return runner.main([args.job] if args.job else [])

    if args.command == "watchdog":
        if args.pid is None or not args.data or not args.entry or not args.root:
            parser.error("watchdog requires --pid, --data, --entry and --root")
        from helix.adapters.watchdog import watchdog_main  # no Qt on this path

        return watchdog_main(args.pid, Path(args.data), Path(args.entry), Path(args.root))

    # Backstop single-instance guard for the `helix` console-script entry, which reaches here WITHOUT
    # going through main.py's gate. Idempotent: on the normal main.py path we already hold the lock, so
    # this is a no-op. (This path skips the voice pre-warm anyway, so guarding here costs nothing extra.)
    # Only the Qt shell gets the QLocalServer activation ping; a web duplicate raises HELIX by opening
    # a tab — and if the backend is actually dead (a quit mid-teardown holds the lock), it WAITS for
    # the lock and boots in the outgoing instance's place instead of leaving nothing running.
    from helix.app.single_instance import become_primary_after_quit, become_primary_or_signal
    from helix.config import AppPaths

    if not become_primary_or_signal(AppPaths.resolve().data, is_relaunch=False,
                                    signal=(args.command == "qt")):
        if args.command == "qt" or args.headless:
            return 0
        if open_running_face():
            return 0  # live HELIX — the click opened (another) tab on it
        if not become_primary_after_quit(AppPaths.resolve().data):
            return 0  # someone healthy really does hold the lock; don't boot a rival

    if args.command in ("ui", "web"):  # the web shell IS the default face now
        from helix.app.webboot import run_web  # no Qt on this path

        # The face lives in the user's own BROWSER by default — HELIX runs as a background presence
        # and the icon opens a tab on it. --window opts back into the WebView2 app window.
        mode = "none" if args.headless else ("window" if args.window else "browser")
        return run_web(mode)

    from helix.app.bootstrap import run_app  # 'qt' — the legacy shell; this pulls in PyQt6

    return run_app()
