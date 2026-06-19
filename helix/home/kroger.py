"""Official Fry's/Kroger API client (urllib) — product search + add-to-cart (§home groceries).

**Add-to-cart only** — Kroger exposes no programmatic checkout, which is exactly the safe behavior we
want: HELIX fills the cart, you tap checkout in the Fry's app. Product search + cart both use a **user**
token (OAuth authorization-code, obtained once via `scripts/kroger_authorize.py`, then refreshed). Creds
live in settings (git-ignored). Mirrors the stdlib-only style of `brokers/alpaca.py`.

Setup (one time): register an app at developer.kroger.com (scopes `product.compact cart.basic:write`),
save the client id/secret + your Fry's location id in settings, then run the authorize script once to
grant cart access — it saves a refresh token. After that HELIX can fill your cart on command.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

KROGER_CLIENT_ID_SETTING = "kroger_client_id"
KROGER_CLIENT_SECRET_SETTING = "kroger_client_secret"
KROGER_LOCATION_SETTING = "kroger_location_id"
KROGER_REFRESH_TOKEN_SETTING = "kroger_refresh_token"
KROGER_SCOPES = "product.compact cart.basic:write"
_BASE = "https://api.kroger.com/v1"


class KrogerError(RuntimeError):
    pass


def kroger_config(settings: Any) -> dict:
    return {
        "client_id": settings.get(KROGER_CLIENT_ID_SETTING, "") or "",
        "client_secret": settings.get(KROGER_CLIENT_SECRET_SETTING, "") or "",
        "location_id": settings.get(KROGER_LOCATION_SETTING, "") or "",
        "refresh_token": settings.get(KROGER_REFRESH_TOKEN_SETTING, "") or "",
    }


def is_configured(settings: Any) -> bool:
    cfg = kroger_config(settings)
    return bool(cfg["client_id"] and cfg["client_secret"] and cfg["refresh_token"])


def _token_request(client_id: str, client_secret: str, form: dict) -> dict:
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        f"{_BASE}/connect/oauth2/token", data=data, method="POST",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise KrogerError(f"Kroger auth {error.code}: {error.read().decode('utf-8', 'replace')[:200]}") from error
    except urllib.error.URLError as error:
        raise KrogerError(f"Kroger connection failed: {error.reason}") from error


def refresh_user_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    """Exchange a refresh token for a fresh (access_token, refresh_token)."""
    body = _token_request(client_id, client_secret, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    return body.get("access_token", ""), body.get("refresh_token", refresh_token)


def search_product(term: str, location_id: str, access_token: str) -> dict | None:
    """Return {'upc', 'description'} for the top match, or None."""
    query = urllib.parse.urlencode(
        {"filter.term": term, "filter.locationId": location_id, "filter.limit": 1}
    )
    request = urllib.request.Request(
        f"{_BASE}/products?{query}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise KrogerError(f"Kroger search {error.code}: {error.read().decode('utf-8', 'replace')[:160]}") from error
    except urllib.error.URLError as error:
        raise KrogerError(f"Kroger connection failed: {error.reason}") from error
    products = data.get("data") or []
    if not products:
        return None
    top = products[0]
    return {"upc": top.get("productId") or top.get("upc"), "description": top.get("description", "")}


def add_to_cart(items: list[dict], access_token: str) -> None:
    """items: [{'upc': str, 'quantity': int}]. PUT /cart/add with the user's Bearer token."""
    body = json.dumps({"items": items}).encode("utf-8")
    request = urllib.request.Request(
        f"{_BASE}/cart/add", data=body, method="PUT",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as error:
        raise KrogerError(f"Kroger cart {error.code}: {error.read().decode('utf-8', 'replace')[:200]}") from error
    except urllib.error.URLError as error:
        raise KrogerError(f"Kroger connection failed: {error.reason}") from error


def order_list(settings: Any, items_to_order: list[dict]) -> dict:
    """Add each shopping-list item to the Fry's cart. Returns {'added': [...], 'missing': [...]}.

    Refreshes the user token (and persists the rotated refresh token), searches each item for its UPC,
    then adds the found items to the cart. Does NOT check out — the user does that in the Fry's app."""
    if not is_configured(settings):
        raise KrogerError("Fry's isn't connected — add the Kroger client id/secret and authorize once.")
    cfg = kroger_config(settings)
    access, new_refresh = refresh_user_token(cfg["client_id"], cfg["client_secret"], cfg["refresh_token"])
    if new_refresh and new_refresh != cfg["refresh_token"]:
        settings.set(KROGER_REFRESH_TOKEN_SETTING, new_refresh)
    if not access:
        raise KrogerError("Couldn't get a Kroger token — re-authorize Fry's.")
    cart_items, added, missing = [], [], []
    for row in items_to_order:
        term = row.get("item", "")
        qty = int(row.get("qty", 1) or 1)
        try:
            found = search_product(term, cfg["location_id"], access)
        except KrogerError:
            found = None
        if found and found.get("upc"):
            cart_items.append({"upc": found["upc"], "quantity": qty})
            added.append(term)
        else:
            missing.append(term)
    if cart_items:
        add_to_cart(cart_items, access)
    return {"added": added, "missing": missing}
