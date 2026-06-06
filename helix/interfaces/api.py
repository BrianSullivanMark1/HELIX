from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from helix.core.memory import SQLiteMemory
from helix.investment.planner import build_briefing


class HelixApiServer:
    def __init__(self, memory: SQLiteMemory, host: str, port: int) -> None:
        self.memory = memory
        self.host = host
        self.port = port

    def serve_forever(self) -> None:
        memory = self.memory

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send_json({"status": "online"})
                    return

                if self.path == "/brief":
                    briefing = build_briefing(memory.get_investment_profile())
                    self._send_json(_briefing_to_payload(briefing))
                    return

                if self.path == "/watchlist":
                    self._send_json({"items": memory.list_watchlist()})
                    return

                self._send_json({"error": "not found"}, status=404)

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload, indent=2).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        print(f"HELIX API online at http://{self.host}:{self.port}")
        print("Endpoints: /health, /brief, /watchlist")
        print("Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()
            print("HELIX API stopped.")
        finally:
            server.server_close()


def _briefing_to_payload(briefing: Any) -> dict[str, Any]:
    return {
        "profile_exists": briefing.profile_exists,
        "monthly_surplus": briefing.monthly_surplus,
        "emergency_target": briefing.emergency_target,
        "emergency_gap": briefing.emergency_gap,
        "investable_cash_now": briefing.investable_cash_now,
        "monthly_investment_target": briefing.monthly_investment_target,
        "projected_goal_value": briefing.projected_goal_value,
        "required_monthly_contribution": briefing.required_monthly_contribution,
        "allocation": briefing.allocation or {},
        "next_action": briefing.next_action,
    }
