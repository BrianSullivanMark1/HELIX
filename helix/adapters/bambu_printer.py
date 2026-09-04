"""BambuPrinter — the P1S over LAN mode: status by MQTT, files by FTPS, print-start by MQTT.

Design-to-plastic, the last mile. Bambu's LAN mode (no cloud account) speaks two local protocols,
both authenticated with the access code off the printer's own screen:

  * MQTT on 8883 (TLS, self-signed — the printer IS the trust anchor, so verification is off and
    the access code is the secret): user "bblp", the report stream on device/<serial>/report and
    commands on device/<serial>/request.
  * implicit FTPS on 990 for the SD card: sliced .gcode.3mf files uploaded here can be started by
    the project_file MQTT command.

Slicing itself belongs to Bambu Studio. try_slice() attempts the Studio CLI headlessly; when that
isn't available (older Studio builds have no usable CLI), open_in_studio() loads the model into the
Studio GUI so the user presses Print — and the MQTT status path works either way, which is where
the daily value lives ("how's the print going?" from the couch).

Everything here raises BambuError with a plain sentence; the tool layer turns those into
conversation. Nothing imports paho at module load — a build without it degrades to a friendly
message, not an ImportError at boot.
"""
from __future__ import annotations

import ftplib
import json
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("bambu")

_MQTT_PORT = 8883
_FTPS_PORT = 990
_USER = "bblp"

# Bambu Studio's stock install locations (the CLI probe and the GUI fallback both use this).
_STUDIO_PATHS = (
    r"C:\Program Files\Bambu Studio\bambu-studio.exe",
    r"C:\Program Files\BambuStudio\bambu-studio.exe",
)

# gcode_state values the printer reports, mapped to plain words for the status line.
_STATE_WORDS = {
    "IDLE": "idle", "RUNNING": "printing", "PAUSE": "paused", "FINISH": "finished",
    "FAILED": "failed", "PREPARE": "preparing", "SLICING": "slicing",
}


class BambuError(Exception):
    """A printer problem in plain words — the tool layer relays it verbatim."""


def _mqtt_client(host: str, access_code: str):
    try:
        import paho.mqtt.client as mqtt
    except Exception as exc:  # noqa: BLE001
        raise BambuError(
            "The MQTT library (paho-mqtt) isn't in this build — rebuild HELIX to talk to the printer."
        ) from exc
    try:  # paho 2.x wants the callback API version named; 1.x has no such argument
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    except AttributeError:
        client = mqtt.Client(protocol=mqtt.MQTTv311)
    client.username_pw_set(_USER, access_code)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the printer's cert is self-signed; the access code is the auth
    client.tls_set_context(ctx)
    return client


class _ImplicitFTPS(ftplib.FTP_TLS):
    """ftplib speaks explicit FTPS (AUTH TLS after a plain hello); the printer wants IMPLICIT —
    TLS from the very first byte on port 990 — so connect() wraps the socket immediately."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        self.host, self.port = host or self.host, port or self.port
        if timeout != -999:
            self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.af = self.sock.family
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


class BambuPrinter:
    """One printer, addressed by the three values off its own screen (host, access code, serial)."""

    def __init__(self, host: str, access_code: str, serial: str) -> None:
        self._host = (host or "").strip()
        self._code = (access_code or "").strip()
        self._serial = (serial or "").strip()
        if not (self._host and self._code and self._serial):
            raise BambuError("The printer isn't connected — ask HELIX to connect the Bambu printer.")

    # ----- MQTT -----
    def _request(self, payload: dict, *, want_report: bool = False, timeout: float = 12.0) -> dict:
        """Publish one command; optionally wait for the next status report. Returns the report's
        'print' dict ({} when not asked to wait)."""
        client = _mqtt_client(self._host, self._code)
        got: dict = {}
        done = threading.Event()

        def _on_connect(cl, *args, **kw):
            cl.subscribe(f"device/{self._serial}/report")
            cl.publish(f"device/{self._serial}/request", json.dumps(payload))
            if not want_report:
                done.set()

        def _on_message(cl, _userdata, msg, *args, **kw):
            try:
                data = json.loads(msg.payload.decode("utf-8", "replace"))
            except ValueError:
                return
            p = data.get("print")
            if isinstance(p, dict) and ("gcode_state" in p or "mc_percent" in p):
                got.update(p)
                done.set()

        client.on_connect = _on_connect
        client.on_message = _on_message
        try:
            client.connect(self._host, _MQTT_PORT, keepalive=30)
        except Exception as exc:  # noqa: BLE001
            raise BambuError(
                f"Couldn't reach the printer at {self._host} — is it on, on this network, "
                "with LAN mode enabled?"
            ) from exc
        client.loop_start()
        try:
            if not done.wait(timeout):
                raise BambuError(
                    "The printer accepted the connection but never answered — check the access "
                    "code and serial number."
                )
        finally:
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return got

    def status(self) -> dict:
        """One live status report: state, percent, minutes remaining, job name, temperatures."""
        p = self._request({"pushing": {"command": "pushall", "sequence_id": "1"}},
                          want_report=True)
        state = str(p.get("gcode_state") or "").upper()
        return {
            "state": _STATE_WORDS.get(state, state.lower() or "unknown"),
            "percent": p.get("mc_percent"),
            "minutes_left": p.get("mc_remaining_time"),
            "job": p.get("subtask_name") or p.get("gcode_file") or "",
            "nozzle_c": p.get("nozzle_temper"),
            "bed_c": p.get("bed_temper"),
        }

    # ----- FTPS -----
    def upload(self, path: Path, remote_name: str | None = None) -> str:
        """Put one file on the printer's SD card root. Returns the remote name."""
        path = Path(path)
        if not path.is_file():
            raise BambuError(f"There's no file to send at {path.name}.")
        name = remote_name or path.name
        ftp = _ImplicitFTPS()
        ftp.timeout = 30
        try:
            ftp.connect(self._host, _FTPS_PORT, timeout=30)
            ftp.login(_USER, self._code)
            ftp.prot_p()  # encrypt the data channel too — the control channel already is
            with path.open("rb") as fh:
                ftp.storbinary(f"STOR {name}", fh)
        except BambuError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BambuError(
                f"Couldn't upload to the printer's SD card ({exc.__class__.__name__}) — check the "
                "access code, and that a card is in the printer."
            ) from exc
        finally:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                pass
        return name

    def start_print(self, remote_name: str) -> None:
        """Start a print from a sliced .gcode.3mf already on the SD card."""
        self._request({
            "print": {
                "command": "project_file",
                "param": "Metadata/plate_1.gcode",
                "url": f"file:///sdcard/{remote_name}",
                "subtask_name": remote_name,
                "use_ams": False,
                "timelapse": False,
                "bed_leveling": True,
                "flow_cali": False,
                "vibration_cali": False,
                "layer_inspect": False,
                "sequence_id": "0",
            }
        })


# ----- Bambu Studio (slicing / the GUI fallback) -----
def studio_exe() -> Path | None:
    for p in _STUDIO_PATHS:
        if Path(p).is_file():
            return Path(p)
    return None


def try_slice(model_3mf: Path, out_3mf: Path, timeout_s: float = 240.0) -> bool:
    """Attempt a headless slice through the Studio CLI. False on ANY failure — CLI slicing is
    version-dependent, so this is an attempt, never a promise; the caller falls back to the GUI."""
    exe = studio_exe()
    if exe is None or not Path(model_3mf).is_file():
        return False
    try:
        out_3mf.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [str(exe), "--slice", "0", "--export-3mf", str(out_3mf), str(model_3mf)],
            capture_output=True, timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = proc.returncode == 0 and out_3mf.is_file() and out_3mf.stat().st_size > 0
        if not ok:
            _LOG.info("studio CLI slice declined (rc=%s)", proc.returncode)
        return ok
    except Exception:  # noqa: BLE001
        _LOG.info("studio CLI slice failed", exc_info=True)
        return False


def open_in_studio(path: Path) -> bool:
    """Load the model into the Bambu Studio GUI (the user presses Print there). Detached."""
    exe = studio_exe()
    if exe is None:
        return False
    try:
        subprocess.Popen([str(exe), str(path)], close_fds=True)
        return True
    except Exception:  # noqa: BLE001
        _LOG.warning("could not open Bambu Studio", exc_info=True)
        return False


def format_status(s: dict) -> str:
    """One friendly sentence out of status() — shared by the tool and any future UI chip."""
    state = s.get("state") or "unknown"
    job = f" on '{s['job']}'" if s.get("job") else ""
    bits = []
    if s.get("percent") is not None:
        bits.append(f"{s['percent']}% done")
    m = s.get("minutes_left")
    if m:
        bits.append(f"about {int(m) // 60}h {int(m) % 60}m left" if int(m) >= 60
                    else f"about {int(m)}m left")
    if s.get("nozzle_c") is not None and state in ("printing", "preparing", "paused"):
        bits.append(f"nozzle {round(float(s['nozzle_c']))}°C")
    tail = " — " + ", ".join(bits) if bits else ""
    return f"The printer is {state}{job}{tail}."
