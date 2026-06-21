from __future__ import annotations

import argparse
import sys

from helix import __version__
from helix.core.config import load_config
from helix.core.memory import SQLiteMemory


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None and len(sys.argv) == 1:
        argv = ["ui"]  # bare `helix` opens the desktop app
    args = parser.parse_args(argv)

    config = load_config()
    memory = SQLiteMemory(config.db_path)

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    return handler(args, memory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="helix", description="HELIX — talk an app into existence.")
    parser.add_argument("--version", action="version", version=f"HELIX {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    ui = subparsers.add_parser("ui", help="Open the HELIX desktop app (default)")
    ui.set_defaults(handler=handle_ui)

    return parser


def handle_ui(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    try:
        from helix.interfaces.qt_app import run_qt_app
    except ModuleNotFoundError as error:
        if error.name == "PyQt6":
            print("PyQt6 is required for the desktop UI.")
            print("Install it with: python -m pip install PyQt6")
            return 1
        raise

    return run_qt_app(memory)
