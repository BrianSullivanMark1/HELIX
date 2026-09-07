"""DesktopService — JARVIS-grade control of the user's own machine.

Three small faculties, all user-driven (never available to autonomous agents):
  - open_program: "open Excel" launches an installed program, resolved from the Start Menu / PATH —
    never an arbitrary path, so spoken words can only reach things the user installed.
  - media: play/pause, next, previous, mute, volume up/down — synthesized media-key taps, exactly as
    if pressed on the keyboard; they act on whatever the OS routes media keys to.
  - system_status: cores, memory, disk, battery in one spoken-friendly line (read-only).

Windows-native via ctypes/os.startfile — no new dependencies; degrades to a plain "can't here" off
Windows. Pure-ish (all OS touchpoints injectable) so it is unit-testable without a real desktop.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("desktop")

# Media virtual-key codes (user32 keybd_event) — the same scan the physical keys send.
_MEDIA_VKS: dict[str, int] = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
}
_KEYEVENTF_KEYUP = 0x0002
_VOLUME_TAPS = 4  # one spoken "volume up" nudges by a few OS steps (each tap is tiny)


def _default_start_menu_dirs() -> list[Path]:
    """Where installed programs' shortcuts live (all-users + per-user Start Menu)."""
    dirs: list[Path] = []
    for env, sub in (
        ("ProgramData", "Microsoft/Windows/Start Menu/Programs"),
        ("APPDATA", "Microsoft/Windows/Start Menu/Programs"),
    ):
        base = os.environ.get(env)
        if base:
            p = Path(base) / sub
            if p.is_dir():
                dirs.append(p)
    return dirs


class DesktopService:
    def __init__(self, *, launcher=None, key_tap=None, start_menu_dirs=None, platform=None) -> None:
        # Every OS touchpoint is injectable for tests: launcher(path) opens a file/shortcut,
        # key_tap(vk) taps one key, start_menu_dirs are the shortcut roots.
        self._launch = launcher or os.startfile  # noqa: S606 - launching the user's own programs is the feature
        self._key_tap = key_tap or self._tap_windows_key
        self._menu_dirs = start_menu_dirs if start_menu_dirs is not None else _default_start_menu_dirs()
        self._platform = platform or sys.platform

    # ----- open a program by its everyday name -----
    def open_program(self, name: str) -> str:
        """Launch an INSTALLED program by name ("excel", "notepad", "chrome"). Resolution order:
        a Start Menu shortcut whose name matches, then a bare command on PATH. Never a path — a
        spoken name with slashes (or drive colons) is refused, so this can only reach what's
        installed, not run an arbitrary file."""
        if self._platform != "win32":
            return "Opening programs is only supported on Windows."
        name = " ".join((name or "").strip().split())
        if not name:
            return "Which program should I open?"
        if any(ch in name for ch in ("/", "\\", ":")):
            return "I can only open installed programs by name, not paths."
        lnk = self._find_shortcut(name)
        if lnk is not None:
            try:
                self._launch(str(lnk))
                return f"Opening {lnk.stem}."
            except OSError as exc:
                _LOG.warning("could not launch %s: %s", lnk, exc)
                return f"I found {lnk.stem} but couldn't launch it."
        exe = shutil.which(name)
        if exe:
            try:
                self._launch(exe)
                return f"Opening {name}."
            except OSError as exc:
                _LOG.warning("could not launch %s: %s", exe, exc)
                return f"I found {name} but couldn't launch it."
        return f"I couldn't find a program called '{name}' installed here."

    def _find_shortcut(self, name: str) -> Path | None:
        """The best-matching Start Menu .lnk: exact stem match wins, then a stem starting with the
        name, then a substring hit — shallow-first so 'Excel' beats a nested 'Excel Viewer Docs'."""
        needle = name.lower()
        exact: Path | None = None
        starts: Path | None = None
        contains: Path | None = None
        for root in self._menu_dirs:
            try:
                links = sorted(root.rglob("*.lnk"), key=lambda p: len(p.parts))
            except OSError:
                continue
            for p in links:
                stem = p.stem.lower()
                if stem == needle:
                    exact = exact or p
                elif stem.startswith(needle):
                    starts = starts or p
                elif needle in stem:
                    contains = contains or p
            if exact is not None:
                break
        return exact or starts or contains

    # ----- media keys -----
    def media(self, action: str) -> str:
        """Tap a media key: play_pause, next, previous, mute, volume_up, volume_down. Volume nudges
        tap a few OS steps so one spoken request makes an audible difference."""
        if self._platform != "win32":
            return "Media keys are only supported on Windows."
        key = (action or "").strip().lower().replace(" ", "_").replace("-", "_")
        vk = _MEDIA_VKS.get(key)
        if vk is None:
            return f"I don't know the media action '{action}'. I can do: {', '.join(sorted(_MEDIA_VKS))}."
        taps = _VOLUME_TAPS if key in ("volume_up", "volume_down") else 1
        try:
            for _ in range(taps):
                self._key_tap(vk)
        except Exception as exc:  # noqa: BLE001 - a blocked key must never crash a turn
            _LOG.warning("media key %s failed: %s", key, exc)
            return "I couldn't send that media key just now."
        spoken = {
            "play_pause": "Toggled playback.", "next": "Next track.", "previous": "Previous track.",
            "mute": "Toggled mute.", "volume_up": "Volume up.", "volume_down": "Volume down.",
        }
        return spoken[key]

    @staticmethod
    def _tap_windows_key(vk: int) -> None:
        import ctypes

        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)

    # ----- one spoken-friendly machine status line -----
    def system_status(self) -> str:
        """Cores, memory, disk, battery — one plain line the voice can read as-is."""
        parts: list[str] = []
        cores = os.cpu_count()
        if cores:
            parts.append(f"{cores} cores")
        mem = self._memory_line()
        if mem:
            parts.append(mem)
        try:
            du = shutil.disk_usage(Path.home().anchor or "/")
            parts.append(f"disk {du.free / 1e9:.0f} of {du.total / 1e9:.0f} gigabytes free")
        except OSError:
            pass
        battery = self._battery_line()
        if battery:
            parts.append(battery)
        return ("The machine: " + ", ".join(parts) + ".") if parts else "I couldn't read the machine status."

    def _memory_line(self) -> str:
        if self._platform != "win32":
            return ""
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return (f"memory {st.dwMemoryLoad} percent used "
                        f"({st.ullAvailPhys / 1e9:.0f} of {st.ullTotalPhys / 1e9:.0f} gigabytes free)")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _battery_line(self) -> str:
        if self._platform != "win32":
            return ""
        try:
            import ctypes

            class _PowerStatus(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32),
                ]

            st = _PowerStatus()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
                if st.BatteryFlag == 128 or st.BatteryLifePercent > 100:  # no battery / unknown
                    return ""
                plugged = " and charging" if st.ACLineStatus == 1 else ""
                return f"battery {st.BatteryLifePercent} percent{plugged}"
        except Exception:  # noqa: BLE001
            pass
        return ""
