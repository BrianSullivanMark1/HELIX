"""One-time Fry's/Kroger authorization (§home groceries). Run once after registering a Kroger dev app:

    python scripts/kroger_authorize.py

Needs the client id/secret saved in settings (or it prompts). Opens your browser to grant cart access,
captures the code on a localhost redirect, exchanges it for tokens, and saves the **refresh token** so
HELIX can fill your Fry's cart on command. Register the redirect URI below in your Kroger app. Stdlib only.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helix.core.settings import AppSettings
from helix.home.kroger import (
    KROGER_CLIENT_ID_SETTING,
    KROGER_CLIENT_SECRET_SETTING,
    KROGER_LOCATION_SETTING,
    KROGER_REFRESH_TOKEN_SETTING,
    KROGER_SCOPES,
)

REDIRECT_URI = "http://localhost:8731/callback"
AUTHORIZE_URL = "https://api.kroger.com/v1/connect/oauth2/authorize"
TOKEN_URL = "https://api.kroger.com/v1/connect/oauth2/token"


def main() -> int:
    settings = AppSettings()
    client_id = settings.get(KROGER_CLIENT_ID_SETTING) or input("Kroger client id: ").strip()
    client_secret = settings.get(KROGER_CLIENT_SECRET_SETTING) or input("Kroger client secret: ").strip()
    location = settings.get(KROGER_LOCATION_SETTING) or input("Fry's location id (optional): ").strip()
    settings.set(KROGER_CLIENT_ID_SETTING, client_id)
    settings.set(KROGER_CLIENT_SECRET_SETTING, client_secret)
    if location:
        settings.set(KROGER_LOCATION_SETTING, location)

    params = urllib.parse.urlencode(
        {"scope": KROGER_SCOPES, "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI}
    )
    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.urlparse(self.path).query
            captured["code"] = urllib.parse.parse_qs(query).get("code", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"HELIX: Fry's connected. You can close this tab.")

        def log_message(self, *_args):  # silence the server log
            pass

    server = HTTPServer(("localhost", 8731), Handler)
    print(f"Opening your browser to authorize Fry's...\nIf it doesn't open: {AUTHORIZE_URL}?{params}")
    webbrowser.open(f"{AUTHORIZE_URL}?{params}")
    server.handle_request()  # block for the single redirect

    code = captured.get("code")
    if not code:
        print("No authorization code received.")
        return 1
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    ).encode()
    request = urllib.request.Request(
        TOKEN_URL, data=data, method="POST",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    refresh = body.get("refresh_token", "")
    if not refresh:
        print("No refresh token returned:", body)
        return 1
    settings.set(KROGER_REFRESH_TOKEN_SETTING, refresh)
    print("Fry's connected. Refresh token saved — HELIX can now fill your cart on command.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
