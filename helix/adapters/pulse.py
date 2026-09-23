"""THE PULSE - what HELIX's own Python is costing this PC, measured, not guessed.

A sampler thread reads the process's CPU time every two seconds and turns it into a percent of
one core and of the whole machine; adapters that spawn programs (gcloud, dev.ps1, firebase)
report each spawn here, so the last minute's spawn count sits beside the CPU number. The
frame-rate badge shows it; `GET /api/pulse` answers it. No dependency: time.process_time and
os.cpu_count; memory through psutil when it happens to be installed, else "?".

Why: 2026-09-21, Brian's PC showed 100% CPU with HELIX open. The culprit was a fleet read
spawning gcloud (a Python program) twenty times, not the face - but nobody could tell from the
outside. This makes it visible from the inside.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

SAMPLE_S = 2.0


class Pulse:
    def __init__(self, *, clock=time.monotonic, cpu_time=time.process_time, cores: int | None = None) -> None:
        self._clock = clock
        self._cpu = cpu_time
        self._cores = max(1, cores or (os.cpu_count() or 1))
        self._lock = threading.Lock()
        self._spawns: deque[tuple[float, str]] = deque(maxlen=400)
        self._last = (clock(), cpu_time())
        self._core_pct = 0.0
        self._peak = 0.0
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- feeding it
    def spawned(self, what: str) -> None:
        with self._lock:
            self._spawns.append((self._clock(), what))

    def sample(self) -> float:
        """One reading: this process's CPU as a percent of ONE core since the last sample."""
        now, cpu = self._clock(), self._cpu()
        with self._lock:
            t0, c0 = self._last
            self._last = (now, cpu)
            dt = max(1e-6, now - t0)
            self._core_pct = max(0.0, (cpu - c0) / dt * 100.0)
            self._peak = max(self._peak, self._core_pct)
            return self._core_pct

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                time.sleep(SAMPLE_S)
                try:
                    self.sample()
                except Exception:  # noqa: BLE001
                    pass

        self._thread = threading.Thread(target=loop, daemon=True, name="helix-pulse")
        self._thread.start()

    # ---------------------------------------------------------------- reading it
    def read(self) -> dict:
        now = self._clock()
        with self._lock:
            recent = [w for t, w in self._spawns if now - t <= 60.0]
            core = self._core_pct
            peak = self._peak
        by: dict[str, int] = {}
        for w in recent:
            by[w] = by.get(w, 0) + 1
        rss = None
        try:
            import psutil  # noqa: PLC0415 - optional
            rss = int(psutil.Process().memory_info().rss // (1024 * 1024))
        except Exception:  # noqa: BLE001
            pass
        return {"cpu_core_pct": round(core, 1), "cpu_machine_pct": round(core / self._cores, 1),
                "cpu_peak_core_pct": round(peak, 1), "cores": self._cores, "threads": threading.active_count(),
                "spawns_last_minute": len(recent), "spawns_by": by, "rss_mb": rss}


PULSE = Pulse()


def spawned(what: str) -> None:
    """For adapters: one line, no import cycle worth thinking about."""
    try:
        PULSE.spawned(what)
    except Exception:  # noqa: BLE001
        pass
