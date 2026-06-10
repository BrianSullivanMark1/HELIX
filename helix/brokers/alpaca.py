from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from helix.core.settings import AppSettings


ALPACA_API_KEY_SETTING = "alpaca_api_key"
ALPACA_SECRET_KEY_SETTING = "alpaca_secret_key"
ALPACA_ENVIRONMENT_SETTING = "alpaca_environment"
ALPACA_ENV_PAPER = "Paper"
ALPACA_ENV_LIVE = "Live"

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_BASE_URL = "https://api.alpaca.markets"
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets"


class AlpacaError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str
    secret_key: str
    environment: str = ALPACA_ENV_PAPER

    @property
    def base_url(self) -> str:
        if self.environment == ALPACA_ENV_LIVE:
            return ALPACA_LIVE_BASE_URL
        return ALPACA_PAPER_BASE_URL


class AlpacaClient:
    def __init__(self, credentials: AlpacaCredentials, timeout_seconds: int = 30) -> None:
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: AppSettings | None = None) -> "AlpacaClient":
        settings = settings or AppSettings()
        api_key = settings.get(ALPACA_API_KEY_SETTING, "")
        secret_key = settings.get(ALPACA_SECRET_KEY_SETTING, "")
        environment = settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
        if not api_key or not secret_key:
            raise AlpacaError("Alpaca is not configured. Save an API key and secret first.")
        return cls(
            AlpacaCredentials(
                api_key=api_key,
                secret_key=secret_key,
                environment=environment,
            )
        )

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def get_clock(self) -> dict[str, Any]:
        response = self._request("GET", "/v2/clock")
        return response if isinstance(response, dict) else {}

    def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        """Official market calendar between `start` and `end` (YYYY-MM-DD): one entry per *trading*
        day with ET `open`/`close` times. Weekends and holidays are simply absent; early-close days
        (e.g. day after Thanksgiving) carry the actual `close` (e.g. "13:00")."""
        response = self._request("GET", "/v2/calendar", {"start": start, "end": end})
        return response if isinstance(response, list) else []

    def get_positions(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v2/positions")
        return response if isinstance(response, list) else []

    def get_assets(self, status: str = "active", asset_class: str = "us_equity") -> list[dict[str, Any]]:
        """The broker's master asset list — every tradeable US equity (§36). One free call returning
        ~thousands of {symbol, name, exchange, status, tradable, fractionable, …}. This is the
        authoritative 'real, tradeable market ticker' universe HELIX validates discovered names against."""
        response = self._request("GET", "/v2/assets", {"status": status, "asset_class": asset_class})
        return response if isinstance(response, list) else []

    def get_open_orders(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v2/orders", {"status": "open", "limit": "50"})
        return response if isinstance(response, list) else []

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> dict[str, Any]:
        """Account equity time series. `period` is <n><D|W|M|A>; `timeframe` is 1Min/5Min/15Min/1H/1D.

        Alpaca requires 1D timeframe for periods longer than 30 days. Returns parallel arrays
        `timestamp` (epoch seconds), `equity`, `profit_loss`, `profit_loss_pct` plus `base_value`;
        equity entries may be null for gaps.
        """
        response = self._request(
            "GET", "/v2/account/portfolio/history", {"period": period, "timeframe": timeframe}
        )
        return response if isinstance(response, dict) else {}

    def get_stock_bars(
        self,
        symbol: str,
        timeframe: str = "1Day",
        start: str | None = None,
        limit: int = 10000,
        feed: str = "iex",
    ) -> dict[str, Any]:
        """Historical OHLC bars from Alpaca's market-data API (different host).

        Used for the S&P 500 benchmark line (§19). `timeframe` uses the data-API format
        (`1Day`, `1Hour`, ...); free/paper accounts must use `feed="iex"` (sip is paid).
        Response: {"bars": {"SYMBOL": [{"t","o","h","l","c","v"}, ...]}}.
        """
        query: dict[str, str] = {
            "symbols": symbol.strip().upper(),
            "timeframe": timeframe,
            "limit": str(int(limit)),
            "feed": feed,
            "sort": "asc",
        }
        if start:
            query["start"] = start
        response = self._request("GET", "/v2/stocks/bars", query, base=ALPACA_DATA_BASE_URL)
        return response if isinstance(response, dict) else {}

    def get_bars_multi(
        self,
        symbols: list,
        timeframe: str = "1Week",
        start: str | None = None,
        limit: int = 10000,
        feed: str = "iex",
    ) -> dict[str, Any]:
        """OHLC bars for MANY symbols in one call (data API), returned as {SYMBOL: [bars…]} oldest
        first. Follows `next_page_token` so the whole window comes back. Used to ground the AI
        research in live price action (momentum, 52-wk position, trend). Free/paper = `feed="iex"`."""
        joined = ",".join(str(s).strip().upper() for s in (symbols or []) if str(s).strip())
        if not joined:
            return {}
        out: dict[str, list] = {}
        page_token: str | None = None
        for _ in range(25):  # safety cap on pagination
            query: dict[str, str] = {
                "symbols": joined,
                "timeframe": timeframe,
                "limit": str(int(limit)),
                "feed": feed,
                "sort": "asc",
            }
            if start:
                query["start"] = start
            if page_token:
                query["page_token"] = page_token
            response = self._request("GET", "/v2/stocks/bars", query, base=ALPACA_DATA_BASE_URL)
            if not isinstance(response, dict):
                break
            for symbol, bars in (response.get("bars") or {}).items():
                out.setdefault(str(symbol).upper(), []).extend(bars or [])
            page_token = response.get("next_page_token")
            if not page_token:
                break
        return out

    def get_news(self, symbols: list | None = None, limit: int = 50, start: str | None = None) -> list:
        """Recent market news (data API `/v1beta1/news`, free on paper), newest first. Returns a
        list of {headline, summary, source, created_at, symbols}. Optional `symbols` filter."""
        query: dict[str, str] = {"limit": str(int(limit)), "sort": "desc", "exclude_contentless": "true"}
        if symbols:
            query["symbols"] = ",".join(str(s).strip().upper() for s in symbols if str(s).strip())
        if start:
            query["start"] = start
        response = self._request("GET", "/v1beta1/news", query, base=ALPACA_DATA_BASE_URL)
        if isinstance(response, dict):
            news = response.get("news")
            return news if isinstance(news, list) else []
        return []

    def submit_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        qty: float | None = None,
        notional: float | None = None,
    ) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        side = side.strip().lower()
        order_type = order_type.strip().lower()
        time_in_force = time_in_force.strip().lower()

        if not symbol:
            raise AlpacaError("Order symbol is required.")
        if side not in {"buy", "sell"}:
            raise AlpacaError("Order side must be buy or sell.")
        if bool(qty) == bool(notional):
            raise AlpacaError("Provide exactly one order amount: shares or dollars.")

        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
            "client_order_id": f"helix-{uuid.uuid4().hex[:24]}",
        }
        if qty:
            payload["qty"] = str(qty)
        if notional:
            payload["notional"] = str(round(notional, 2))

        response = self._request("POST", "/v2/orders", body=payload)
        return response if isinstance(response, dict) else {}

    def close_position(self, symbol: str, *, percentage: float | None = None) -> dict[str, Any]:
        """Close (or partially close) a position via DELETE /v2/positions/{symbol} (§42). Used to SELL
        NON-FRACTIONABLE names, which can't take a notional sell — Alpaca liquidates whole shares. With
        `percentage` (0-100) it trims that share of the position; without it, it closes the whole position."""
        symbol = symbol.strip().upper()
        if not symbol:
            raise AlpacaError("Symbol is required to close a position.")
        query = None
        if percentage is not None:
            query = {"percentage": str(round(max(0.0, min(100.0, float(percentage))), 2))}
        response = self._request("DELETE", f"/v2/positions/{symbol}", query=query)
        return response if isinstance(response, dict) else {}

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        base: str | None = None,
    ) -> Any:
        url = (base or self.credentials.base_url) + path
        if query:
            url += "?" + urllib.parse.urlencode(query)

        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "APCA-API-KEY-ID": self.credentials.api_key,
                "APCA-API-SECRET-KEY": self.credentials.secret_key,
            },
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise AlpacaError(f"Alpaca API error {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise AlpacaError(f"Alpaca API connection failed: {error.reason}") from error

        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise AlpacaError("Alpaca returned invalid JSON.") from error
