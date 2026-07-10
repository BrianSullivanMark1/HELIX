"""CLI — argparse entry. Bare `helix` opens the desktop app; `helix watchdog ...` runs the tiny
crash-guard process (spawned by bootstrap — never run by hand), which must import no Qt."""
from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="helix", description="HELIX — a local-first desktop app-builder you talk to."
    )
    parser.add_argument(
        "command", nargs="?", default="ui", choices=["ui", "watchdog"],
        help="what to run (default: ui)",
    )
    parser.add_argument("--pid", type=int, help="(watchdog) the HELIX process to guard")
    parser.add_argument("--data", help="(watchdog) the data directory")
    parser.add_argument("--entry", help="(watchdog) the entry script to relaunch")
    parser.add_argument("--root", help="(watchdog) the app root / working directory")
    args = parser.parse_args(argv)

    if args.command == "watchdog":
        if args.pid is None or not args.data or not args.entry or not args.root:
            parser.error("watchdog requires --pid, --data, --entry and --root")
        from helix.adapters.watchdog import watchdog_main  # no Qt on this path

        return watchdog_main(args.pid, Path(args.data), Path(args.entry), Path(args.root))

    # Backstop single-instance guard for the `helix` console-script entry, which reaches here WITHOUT
    # going through main.py's gate. Idempotent: on the normal main.py path we already hold the lock, so
    # this is a no-op. (This path skips the voice pre-warm anyway, so guarding here costs nothing extra.)
    from helix.app.single_instance import become_primary_or_signal
    from helix.config import AppPaths

    if not become_primary_or_signal(AppPaths.resolve().data, is_relaunch=False):
        return 0

    from helix.app.bootstrap import run_app  # imported lazily — this pulls in PyQt6

    return run_app()
