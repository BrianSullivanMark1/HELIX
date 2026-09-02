"""Media sense — is THIS machine audibly playing sound (YouTube, music, a video) right now?

The default render endpoint's peak meter is ground truth for "the speakers are making noise": when it
is hot, whatever the mic hears is (at least partly) the machine's own playback — so the voice layer
holds any speech it cannot attribute to a person (voice.py's playback gate: the thalamic
cocktail-party rule, extended to loudspeakers).

Pure ctypes over WASAPI (IMMDeviceEnumerator → IAudioMeterInformation) — no new dependency, no audio
capture, one cheap COM read per mic chunk. Every failure path (non-Windows, no output device, COM
refused) degrades permanently to "not playing", which is exactly the behavior before media sense
existed. All calls happen on the Qt main thread (the voice controller's slots), one COM apartment.
"""
from __future__ import annotations

import ctypes
import platform
import time
from typing import Callable

from helix.logging_setup import get_logger

_LOG = get_logger("mediasense")

# Endpoint peak (0..1) that counts as "audibly playing". Meter noise and idle hiss sit well below;
# playback loud enough for the MIC to also pick up (the problem case) sits well above.
PEAK_FLOOR = 0.05
# How long after the last hot sample playback still counts as active — bridges brief in-song dips
# without holding the gate long after playback truly stops.
HOT_WINDOW_S = 1.2
# Re-resolve the DEFAULT endpoint this often: a meter is bound to whatever device was default when it
# was built, and a plain default-output switch (dock, Bluetooth, Settings) does NOT invalidate the old
# endpoint — GetPeakValue keeps happily reading the now-idle device. Periodic rebinding picks the new
# default up within seconds, without the complexity of a ctypes IMMNotificationClient callback object.
REBIND_S = 10.0
# After a FAILED (re)build — e.g. zero render endpoints mid device-transition — wait this long before
# trying again, so a missing device costs one cheap failed COM call per few seconds, not per mic chunk.
RETRY_S = 5.0

_CLSCTX_ALL = 0x17
_E_RENDER = 0    # EDataFlow eRender — the machine's OUTPUT (what the speakers play)
_E_CONSOLE = 0   # ERole eConsole — any role resolves the same default endpoint for metering


class _GUID(ctypes.Structure):
    _fields_ = (
        ("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
        ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8),
    )


def _guid(s: str) -> _GUID:
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


def _method(ptr, index: int, *argtypes):
    """COM vtable slot `index` of interface `ptr`, as a callable taking (this, *args) → HRESULT."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def _release(ptr) -> None:
    try:
        if ptr:
            _method(ptr, 2)(ptr)  # IUnknown::Release
    except Exception:  # noqa: BLE001 — releasing a dying interface must never raise into voice
        pass


_COM_READY = False


def _ensure_com() -> None:
    """CoInitialize the calling thread ONCE per process — every rebind reuses it, so the COM init
    refcount never creeps up with the periodic meter rebuilds."""
    global _COM_READY
    if _COM_READY:
        return
    try:
        # Apartment-threaded, matching Qt's STA on the GUI thread; S_FALSE (already done) is fine.
        ctypes.oledll.ole32.CoInitializeEx(None, 0x2)
    except OSError:
        pass  # initialized in another mode already — COM is still usable on this thread
    _COM_READY = True


class _RenderMeter:
    """The default render endpoint's IAudioMeterInformation. Raises on any setup failure; the owner
    (MediaSense) retries later rather than giving up."""

    def __init__(self) -> None:
        ole32 = ctypes.oledll.ole32
        _ensure_com()
        enum = ctypes.c_void_p()
        ole32.CoCreateInstance(
            ctypes.byref(_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")),  # CLSID_MMDeviceEnumerator
            None, _CLSCTX_ALL,
            ctypes.byref(_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")),  # IID_IMMDeviceEnumerator
            ctypes.byref(enum),
        )
        dev = ctypes.c_void_p()
        self._meter = ctypes.c_void_p()
        try:
            hr = _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                enum, _E_RENDER, _E_CONSOLE, ctypes.byref(dev))  # GetDefaultAudioEndpoint
            if hr or not dev:
                raise OSError(f"GetDefaultAudioEndpoint failed (0x{hr & 0xFFFFFFFF:08X})")
            hr = _method(
                dev, 3, ctypes.POINTER(_GUID), ctypes.c_uint32, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(
                dev,
                ctypes.byref(_guid("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")),  # IID_IAudioMeterInformation
                _CLSCTX_ALL, None, ctypes.byref(self._meter),
            )  # IMMDevice::Activate
            if hr or not self._meter:
                raise OSError(f"Activate(IAudioMeterInformation) failed (0x{hr & 0xFFFFFFFF:08X})")
        finally:
            _release(dev)
            _release(enum)

    def peak(self) -> float:
        val = ctypes.c_float(0.0)
        hr = _method(self._meter, 3, ctypes.POINTER(ctypes.c_float))(self._meter, ctypes.byref(val))
        if hr:  # e.g. the default output device changed/vanished (AUDCLNT_E_DEVICE_INVALIDATED)
            raise OSError(f"GetPeakValue failed (0x{hr & 0xFFFFFFFF:08X})")
        return float(val.value)

    def close(self) -> None:
        _release(self._meter)
        self._meter = None


class MediaSense:
    """Playback awareness for the voice layer. tick() cheaply samples the render meter (the controller
    calls it per mic chunk); playing() answers "is the machine audibly playing right now, or was it a
    moment ago?" — a fresh sample plus the recently-hot window, so a brief dip inside a song doesn't
    read as silence.

    `peak_fn` injects a fake meter for tests. Resilience contract: the meter is REBOUND to the default
    endpoint every REBIND_S (a default-output switch never silently strands it on the old device), a
    runtime failure (device invalidated) drops it for an immediate rebuild, and a FAILED build (no
    endpoint mid device-transition, audio service busy at launch) just retries after RETRY_S — it is
    never treated as permanent. Only a non-Windows host is dark for good. Every failure path reads as
    "not playing", which is exactly the behavior before media sense existed. One deliberate blind
    spot: the meter can't tell speakers from HEADPHONES, so playback into headphones (which the mic
    can't hear) still tightens the gate — the cost is saying the name; never a missed protection.
    """

    def __init__(self, peak_fn: Callable[[], float] | None = None) -> None:
        self._peak_fn = peak_fn
        self._meter: _RenderMeter | None = None
        self._dead = peak_fn is None and platform.system() != "Windows"
        self._last_hot = float("-inf")
        self._bound_at = float("-inf")   # when the current meter was bound (drives periodic rebind)
        self._next_retry = float("-inf") # earliest next build attempt after a failed one
        self._warned = False             # log the first build failure once, not per retry

    def _peak(self) -> float:
        if self._peak_fn is not None:
            try:
                return float(self._peak_fn())
            except Exception:  # noqa: BLE001 — a broken injected meter reads as silence
                return 0.0
        if self._dead:
            return 0.0
        now = time.monotonic()
        if self._meter is not None and now - self._bound_at >= REBIND_S:
            self._meter.close()
            self._meter = None  # rebind below, picking up a possibly-changed default output
        if self._meter is None:
            if now < self._next_retry:
                return 0.0
            try:
                first = self._bound_at == float("-inf")
                self._meter = _RenderMeter()
                self._bound_at = now
                if first:
                    _LOG.info("media sense armed — playback-aware listening on")
            except Exception as exc:  # noqa: BLE001
                self._next_retry = now + RETRY_S  # no endpoint right now — try again shortly
                if not self._warned:
                    self._warned = True
                    _LOG.info("media sense meter unavailable (%s) — will keep retrying quietly", exc)
                return 0.0
        try:
            return self._meter.peak()
        except Exception:  # noqa: BLE001 — device invalidated: drop and rebuild on the next tick
            self._meter.close()
            self._meter = None
            return 0.0

    def tick(self) -> None:
        """Sample the render meter once; remember when it was last hot."""
        if self._peak() >= PEAK_FLOOR:
            self._last_hot = time.monotonic()

    def playing(self) -> bool:
        """Is the machine audibly playing (now, or within the recently-hot window)?"""
        self.tick()
        return (time.monotonic() - self._last_hot) <= HOT_WINDOW_S
