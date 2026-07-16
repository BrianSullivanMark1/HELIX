"""RemoteCompanion — the thin HTTP shell for the optional remote companion.

Deliberately under helix/app/ (a PROTECTED path): the bind address, the auth, and the routes live here
+ in services/remote.py, so a self-change can never soften them. All policy is in RemoteService; this
file only moves bytes. Default OFF — start() is a no-op unless the user enabled it in Settings.
"""
from __future__ import annotations

import http.server
import threading

from helix.logging_setup import get_logger

_LOG = get_logger("remote")
_MAX_READ = 65536  # hard cap on bytes read off the wire (RemoteService also caps the logical body)


class _Handler(http.server.BaseHTTPRequestHandler):
    def _dispatch(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(max(0, length), _MAX_READ)) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            client_ip = self.client_address[0] if self.client_address else ""
            status, ctype, out = self.server.service.handle(  # type: ignore[attr-defined]
                method, self.path, headers, body, client_ip
            )
        except Exception:  # noqa: BLE001 — never leak a stack trace to a remote caller
            status, ctype, out = 500, "application/json", b'{"error":"server error"}'
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(out)
        except Exception:  # noqa: BLE001 — client hung up
            pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, *_args) -> None:  # keep the console quiet
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RemoteCompanion:
    def __init__(self, service) -> None:
        self._service = service
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Start listening if the user enabled it. Loopback-only unless they also opted into LAN. Returns
        True if it is now serving."""
        if self._httpd is not None:
            return True
        if not self._service.enabled():
            return False
        self._service.ensure_token()
        try:
            httpd = _Server((self._service.bind_host(), self._service.port()), _Handler)
        except OSError as exc:
            _LOG.warning("remote companion could not bind %s:%s (%s)",
                         self._service.bind_host(), self._service.port(), exc)
            return False
        httpd.service = self._service  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="helix-remote")
        self._thread.start()
        _LOG.info("remote companion listening on %s:%s", self._service.bind_host(), self._service.port())
        return True

    def stop(self) -> None:
        httpd, self._httpd, self._thread = self._httpd, None, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass

    def restart(self) -> None:
        """Re-read the settings (enabled / LAN / port changed) and apply — called after a Settings save."""
        self.stop()
        if self._service.enabled():
            self.start()
