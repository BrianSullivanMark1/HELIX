from __future__ import annotations

import time

from helix.core.memory import SQLiteMemory
from helix.investment.planner import build_briefing, render_briefing


def run_core(memory: SQLiteMemory, interval_seconds: int, once: bool = False) -> int:
    print("HELIX core online.")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            profile = memory.get_investment_profile()
            print()
            print(render_briefing(build_briefing(profile)))

            if once:
                return 0

            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print()
        print("HELIX core stopped.")
        return 0
