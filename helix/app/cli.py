"""CLI — argparse entry. Bare `helix` opens the desktop app."""
from __future__ import annotations

import argparse

from helix.app.bootstrap import run_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="helix", description="HELIX — a local-first desktop app-builder you talk to."
    )
    parser.add_argument(
        "command", nargs="?", default="ui", choices=["ui"], help="what to run (default: ui)"
    )
    parser.parse_args(argv)
    return run_app()
