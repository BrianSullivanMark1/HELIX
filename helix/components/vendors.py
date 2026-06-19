"""DigiKey + Mouser electronics catalog clients (urllib) — part search + order (§components).

Stdlib-only, mirroring `helix/home/kroger.py` and `helix/brokers/alpaca.py`. Two vendors:

- **Mouser** — a single API key (query param). Keyword search + cart insert.
- **DigiKey** — OAuth2 *client-credentials* (a client id + secret → a short-lived bearer token).
  Keyword search v4. Client-credentials covers the catalog (search/pricing/stock); a full
  programmatic checkout needs DigiKey's authorization-code Order API, so HELIX prepares the order and
  records it locally — the user reviews and checks out on the vendor site (same safe shape as Fry's:
  HELIX never silently completes a purchase).

Credentials live in settings (git-ignored). Get them free from a developer account at
developer.digikey.com / mouser.com/api-hub. Nothing here runs without the user adding keys.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

VENDORS = ("digikey", "mouser")
VENDOR_LABELS = {"digikey": "DigiKey", "mouser": "Mouser"}

MOUSER_API_KEY_SETTING = "mouser_api_key"
DIGIKEY_CLIENT_ID_SETTING = "digikey_client_id"
DIGIKEY_CLIENT_SECRET_SETTING = "digikey_client_secret"

_MOUSER_BASE = "https://api.mouser.com/api/v1"
_DIGIKEY_BASE = "https://api.digikey.com"


class VendorError(RuntimeError):
    pass


def normalize_vendor(vendor: str) -> str:
    v = (vendor or "").strip().lower()
    if v in ("digi-key", "digi key", "dk"):
        return "digikey"
    return v


def vendor_label(vendor: str) -> str:
    return VENDOR_LABELS.get(normalize_vendor(vendor), (vendor or "").strip() or "the vendor")


def is_configured(settings: Any, vendor: str) -> bool:
    vendor = normalize_vendor(vendor)
    if vendor == "mouser":
        return bool(settings.get(MOUSER_API_KEY_SETTING, "") or "")
    if vendor == "digikey":
        return bool(
            (settings.get(DIGIKEY_CLIENT_ID_SETTING, "") or "")
            and (settings.get(DIGIKEY_CLIENT_SECRET_SETTING, "") or "")
        )
    return False


# --------------------------------------------------------------------------- #
# Low-level HTTP helpers (urllib, like AlpacaClient/KrogerClient).
# --------------------------------------------------------------------------- #


def _post_json(url: str, body: dict, headers: dict, *, label: str) -> dict:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise VendorError(f"{label} {error.code}: {error.read().decode('utf-8', 'replace')[:200]}") from error
    except urllib.error.URLError as error:
        raise VendorError(f"{label} connection failed: {error.reason}") from error


# --------------------------------------------------------------------------- #
# Mouser — API key in the query string.
# --------------------------------------------------------------------------- #


def _mouser_key(settings: Any) -> str:
    key = settings.get(MOUSER_API_KEY_SETTING, "") or ""
    if not key:
        raise VendorError("Mouser isn't connected — add a Mouser API key in settings.")
    return key


def search_mouser(query: str, settings: Any, limit: int = 5) -> list[dict]:
    key = _mouser_key(settings)
    url = f"{_MOUSER_BASE}/search/keyword?apiKey={urllib.parse.quote(key)}"
    body = {"SearchByKeywordRequest": {"keyword": query, "records": max(1, min(int(limit or 5), 20))}}
    data = _post_json(url, body, {"Content-Type": "application/json", "Accept": "application/json"}, label="Mouser search")
    parts = ((data.get("SearchResults") or {}).get("Parts") or [])
    out: list[dict] = []
    for part in parts[: max(1, min(int(limit or 5), 20))]:
        price = ""
        breaks = part.get("PriceBreaks") or []
        if breaks:
            price = str(breaks[0].get("Price", "") or "")
        out.append(
            {
                "vendor": "mouser",
                "part_number": part.get("MouserPartNumber") or part.get("ManufacturerPartNumber") or "",
                "manufacturer": part.get("Manufacturer", "") or "",
                "description": part.get("Description", "") or "",
                "price": price,
                "stock": str(part.get("AvailabilityInStock", "") or part.get("Availability", "") or ""),
                "url": part.get("ProductDetailUrl", "") or "",
            }
        )
    return out


# --------------------------------------------------------------------------- #
# DigiKey — OAuth2 client-credentials → bearer token, then keyword search v4.
# --------------------------------------------------------------------------- #


def _digikey_token(client_id: str, client_secret: str) -> str:
    form = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}
    ).encode()
    request = urllib.request.Request(
        f"{_DIGIKEY_BASE}/v1/oauth2/token", data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise VendorError(f"DigiKey auth {error.code}: {error.read().decode('utf-8', 'replace')[:200]}") from error
    except urllib.error.URLError as error:
        raise VendorError(f"DigiKey connection failed: {error.reason}") from error
    token = body.get("access_token", "")
    if not token:
        raise VendorError("DigiKey didn't return a token — check the client id/secret.")
    return token


def search_digikey(query: str, settings: Any, limit: int = 5) -> list[dict]:
    client_id = settings.get(DIGIKEY_CLIENT_ID_SETTING, "") or ""
    client_secret = settings.get(DIGIKEY_CLIENT_SECRET_SETTING, "") or ""
    if not (client_id and client_secret):
        raise VendorError("DigiKey isn't connected — add a DigiKey client id and secret in settings.")
    token = _digikey_token(client_id, client_secret)
    limit = max(1, min(int(limit or 5), 20))
    body = {"Keywords": query, "Limit": limit, "Offset": 0}
    headers = {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = _post_json(f"{_DIGIKEY_BASE}/products/v4/search/keyword", body, headers, label="DigiKey search")
    products = data.get("Products") or []
    out: list[dict] = []
    for product in products[:limit]:
        desc = product.get("Description") or {}
        manufacturer = product.get("Manufacturer") or {}
        out.append(
            {
                "vendor": "digikey",
                "part_number": product.get("ManufacturerProductNumber", "") or "",
                "manufacturer": manufacturer.get("Name", "") if isinstance(manufacturer, dict) else str(manufacturer or ""),
                "description": desc.get("ProductDescription", "") if isinstance(desc, dict) else str(desc or ""),
                "price": str(product.get("UnitPrice", "") or ""),
                "stock": str(product.get("QuantityAvailable", "") or ""),
                "url": product.get("ProductUrl", "") or "",
            }
        )
    return out


def search(query: str, vendor: str, settings: Any, limit: int = 5) -> list[dict]:
    """Search one vendor's catalog. Returns top results: part_number, price, stock, description, url."""
    query = (query or "").strip()
    if not query:
        raise VendorError("no search term given")
    vendor = normalize_vendor(vendor)
    if vendor == "mouser":
        return search_mouser(query, settings, limit)
    if vendor == "digikey":
        return search_digikey(query, settings, limit)
    raise VendorError(f"Unknown vendor '{vendor}' — use DigiKey or Mouser.")


# --------------------------------------------------------------------------- #
# Order — confirmed-only. Mouser can insert into the real cart (API key); DigiKey's catalog token
# can't check out, so the order is recorded for review. Either way nothing completes a purchase —
# the user checks out on the vendor site, exactly like Fry's add-to-cart.
# --------------------------------------------------------------------------- #


def _mouser_cart_insert(items: list[dict], key: str) -> None:
    url = (
        f"{_MOUSER_BASE}/cart/items/insert"
        f"?apiKey={urllib.parse.quote(key)}&countryCode=US&currencyCode=USD"
    )
    body = {
        "CartItems": [
            {"MouserPartNumber": r.get("item", ""), "Quantity": int(r.get("qty", 1) or 1)}
            for r in items
        ]
    }
    _post_json(url, body, {"Content-Type": "application/json", "Accept": "application/json"}, label="Mouser cart")


def submit_order(vendor: str, settings: Any, items: list[dict]) -> dict:
    """Send the build list toward a vendor order. Returns {'vendor', 'submitted', 'note'}.

    Confirmed-only at the call sites (GUI dialog + spoken-confirmation gate). Mouser carts the items;
    DigiKey records the order for checkout on digikey.com. Never silently completes a purchase."""
    vendor = normalize_vendor(vendor)
    if vendor not in VENDORS:
        raise VendorError(f"Unknown vendor '{vendor}' — use DigiKey or Mouser.")
    if not items:
        raise VendorError("the components list is empty")
    if not is_configured(settings, vendor):
        raise VendorError(f"{vendor_label(vendor)} isn't connected — add its API credentials in settings.")
    submitted = [str(r.get("item", "")).strip() for r in items if r.get("item")]
    if vendor == "mouser":
        _mouser_cart_insert(items, _mouser_key(settings))
        note = "Added to your Mouser cart — review and check out on mouser.com."
    else:  # digikey
        note = "Order prepared — review and check out on digikey.com (catalog access can't complete checkout)."
    return {"vendor": vendor, "submitted": submitted, "note": note}
