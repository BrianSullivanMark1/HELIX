"""RemoteService — the POLICY + routing for the optional remote companion (check in / trigger from a phone).

This is the security-critical seam, kept socket-free so it's fully unit-testable; the actual listener
(helix/app/remote_companion.py) is a thin shell that just calls handle(). Everything here is fail-closed:

  - OFF by default. The listener is never started unless the user enables it in Settings.
  - Loopback-only bind by default; LAN (0.0.0.0) is a SEPARATE explicit opt-in.
  - Every action runs through the SAME autonomous path an agent uses — run_turn(allow_builds=False,
    persist=False) / agents.run() — so it inherits the BUILD_TOOLS denylist by construction: NO builds,
    self-changes, deletes, renames, file writes, or spends can be triggered remotely. Pull-only; it never
    sends anything outward on the user's behalf.
  - A 256-bit bearer token (header-only — no query/cookie, so no ambient-credential CSRF), compared with
    compare_digest. A Host-header allowlist (IP literals / localhost only) blocks DNS-rebinding. A per-IP
    rate limit and a body-size cap bound abuse.
  - WAN exposure / port-forwarding is explicitly OUT OF SCOPE — for off-LAN access, use a VPN/Tailscale.
"""
from __future__ import annotations

import ipaddress
import json
import secrets as _secrets
import threading
import time
from hmac import compare_digest

from helix.logging_setup import get_logger

_LOG = get_logger("remote")

ENABLED_KEY = "remote_enabled"   # master switch — the listener won't even start unless this is True
LAN_KEY = "remote_lan"           # bind 0.0.0.0 (LAN) instead of 127.0.0.1 (this PC only)
PORT_KEY = "remote_port"
_TOKEN_KEY = "remote_token"      # lives in the secrets store, not settings
DEFAULT_PORT = 8770
_MAX_BODY = 8192                 # a remote request body is tiny (a question / an agent name)
_RATE_MAX = 20                   # requests
_RATE_WINDOW = 10.0             # per this many seconds, per client IP


class RemoteService:
    def __init__(self, settings, secrets, conversation=None, agents=None, queue=None) -> None:
        self._settings = settings
        self._secrets = secrets
        self._conversation = conversation
        self._agents = agents
        self._queue = queue
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # ----- config -----
    def enabled(self) -> bool:
        return bool(self._settings.get(ENABLED_KEY, False))

    def lan(self) -> bool:
        return bool(self._settings.get(LAN_KEY, False))

    def bind_host(self) -> str:
        return "0.0.0.0" if self.lan() else "127.0.0.1"

    def port(self) -> int:
        try:
            return int(self._settings.get(PORT_KEY) or DEFAULT_PORT)
        except (TypeError, ValueError):
            return DEFAULT_PORT

    def token(self) -> str:
        return (self._secrets.get(_TOKEN_KEY) or "").strip()

    def ensure_token(self) -> str:
        """Return the token, minting a fresh 256-bit one on first use. Called when the user enables it."""
        tok = self.token()
        if not tok:
            tok = _secrets.token_urlsafe(32)
            self._secrets.set(_TOKEN_KEY, tok)
        return tok

    # ----- security checks -----
    @staticmethod
    def _host_ok(headers: dict) -> bool:
        """Only accept a Host that is an IP literal or 'localhost' — a DNS-rebinding attacker uses a
        DOMAIN that resolves to a private IP, so rejecting non-IP hosts closes that hole."""
        host = (headers.get("host") or headers.get("Host") or "").strip()
        if not host:
            return False
        if host.startswith("["):                       # IPv6 literal: [::1]:8770
            hostname = host[1:host.index("]")] if "]" in host else host
        else:
            hostname = host.rsplit(":", 1)[0] if ":" in host else host
        if hostname.lower() == "localhost":
            return True
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            return False

    def _authorized(self, headers: dict) -> bool:
        tok = self.token()
        if not tok:
            return False
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        prefix = "bearer "
        if not auth.lower().startswith(prefix):
            return False
        return compare_digest(auth[len(prefix):].strip(), tok)

    def _rate_ok(self, client_ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(client_ip, []) if now - t < _RATE_WINDOW]
            if len(hits) >= _RATE_MAX:
                self._hits[client_ip] = hits
                return False
            hits.append(now)
            self._hits[client_ip] = hits
            return True

    # ----- request handling (called by the listener) -----
    def handle(self, method: str, path: str, headers: dict, body: bytes, client_ip: str = "") -> tuple:
        """(status, content_type, body_bytes). Fail-closed at every step."""
        if not self.enabled():
            return self._resp(404, {"error": "remote access is off"})
        if not self._host_ok(headers):
            return self._resp(403, {"error": "forbidden host"})
        if not self._rate_ok(client_ip):
            return self._resp(429, {"error": "slow down"})
        path = (path or "/").split("?", 1)[0]
        if method == "GET" and path == "/":
            return 200, "text/html; charset=utf-8", _COMPANION_PAGE.encode("utf-8")
        # Everything else needs the token.
        if not self._authorized(headers):
            return self._resp(401, {"error": "unauthorized"})
        if body and len(body) > _MAX_BODY:
            return self._resp(413, {"error": "too large"})
        if method == "GET" and path == "/status":
            return self._resp(200, {"ok": True, "status": self._status_text()})
        if method == "POST" and path == "/ask":
            return self._resp(200, {"reply": self._ask(self._json(body).get("text", ""))})
        if method == "POST" and path == "/agent":
            return self._resp(200, {"report": self._run_agent(self._json(body).get("name", ""))})
        return self._resp(404, {"error": "not found"})

    # ----- actions (all autonomous / read-only — inherit the BUILD_TOOLS fence) -----
    def _ask(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "Ask me something."
        if self._conversation is None:
            return "Not available."
        # allow_builds=False + persist=False: same fence + hermetic posture as an agent run — no build,
        # spend, self-change, delete, rename, or file write can be triggered from here.
        try:
            return self._conversation.run_turn(text, allow_builds=False, persist=False)
        except Exception:  # noqa: BLE001
            _LOG.warning("remote ask failed", exc_info=True)
            return "Something went wrong handling that."

    def _run_agent(self, name: str) -> str:
        name = (name or "").strip()
        if not name or self._agents is None:
            return "Name an agent to run."
        try:
            return self._agents.run(name)
        except Exception:  # noqa: BLE001
            _LOG.warning("remote agent run failed", exc_info=True)
            return "Something went wrong running that."

    def _status_text(self) -> str:
        if self._queue is not None:
            try:
                active = self._queue.active_name()
                if active:
                    return f"Working on {active}."
            except Exception:  # noqa: BLE001
                pass
        return "Here and idle."

    # ----- helpers -----
    @staticmethod
    def _json(body: bytes) -> dict:
        try:
            d = json.loads((body or b"").decode("utf-8", "replace") or "{}")
            return d if isinstance(d, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _resp(status: int, obj: dict) -> tuple:
        return status, "application/json", json.dumps(obj).encode("utf-8")


# A minimal phone-friendly page: paste the token once (kept in the browser), then ask or check status.
_COMPANION_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>HELIX</title>
<style>body{background:#080b0f;color:#dfe</style></head><body>
<div style="max-width:640px;margin:0 auto;padding:20px;color:#dfeef0;font-family:-apple-system,Segoe UI,sans-serif">
<h2 style="color:#3fe0e0">HELIX</h2>
<p style="color:#9fb3ba;font-size:14px">Paste your access token (from Settings on the PC), then ask.</p>
<input id="tok" placeholder="access token" style="width:100%;padding:10px;margin:6px 0;background:#0d141b;color:#dfeef0;border:1px solid #26323b;border-radius:8px">
<textarea id="q" rows="3" placeholder="Ask HELIX…" style="width:100%;padding:10px;margin:6px 0;background:#0d141b;color:#dfeef0;border:1px solid #26323b;border-radius:8px"></textarea>
<button onclick="ask()" style="padding:10px 18px;background:#0d141b;color:#3fe0e0;border:1px solid #3fe0e0;border-radius:8px">Ask</button>
<button onclick="status()" style="padding:10px 18px;background:#0d141b;color:#9fb3ba;border:1px solid #26323b;border-radius:8px">Status</button>
<pre id="out" style="white-space:pre-wrap;margin-top:14px;color:#cfeaea"></pre>
<script>
function tok(){var t=document.getElementById('tok').value.trim();if(t)localStorage.setItem('helixtok',t);return t||localStorage.getItem('helixtok')||'';}
async function call(path,opts){opts=opts||{};opts.headers=Object.assign({'Authorization':'Bearer '+tok()},opts.headers||{});var r=await fetch(path,opts);return r.json();}
async function ask(){document.getElementById('out').textContent='…';try{var j=await call('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:document.getElementById('q').value})});document.getElementById('out').textContent=j.reply||j.error||'';}catch(e){document.getElementById('out').textContent='error';}}
async function status(){try{var j=await call('/status');document.getElementById('out').textContent=j.status||j.error||'';}catch(e){document.getElementById('out').textContent='error';}}
if(localStorage.getItem('helixtok'))document.getElementById('tok').value=localStorage.getItem('helixtok');
</script></div></body></html>"""
