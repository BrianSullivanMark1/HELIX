"""Shopping — the data model for HELIX's Amazon cart faculty. Pure data + pure functions, no I/O.

HELIX does the legwork, never the buying: the model resolves products to ASINs (Amazon's 10-character
product ids), stages them, and the ONE outward act is opening Amazon's long-stable remote add-to-cart
link — which pre-loads Amazon's own cart page in the user's browser. Payment, address, and the place-
order button exist only on Amazon's side, under the user's hands; nothing in this module (or anywhere
in HELIX) can spend money.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

# Amazon's remote add-to-cart form: ASIN.1=…&Quantity.1=…, index incremented per line item. It ADDS
# to whatever is already in the browser's Amazon cart and lands on Amazon's own review page.
# Verified live (Aug 2026): Amazon 302s this to its Associates add-to-cart flow with the full item
# list preserved in the return URL — a freshly signed-in user lands straight on the pre-loaded cart;
# a stale session sees Amazon's OWN sign-in first (never HELIX's), then the same pre-loaded cart.
CART_BASE = "https://www.amazon.com/gp/aws/cart/add.html"

MAX_ITEMS = 40     # keeps the URL comfortably inside every browser/server limit (~30 chars per line)
MAX_QUANTITY = 99  # sanity ceiling; Amazon caps line quantities on its side anyway
MAX_PRICE = 100_000.0  # sanity ceiling for a model-read price (dollars) — beyond this, refuse the read

# The full ASIN shape: exactly 10 characters, letters and digits (B0… for most products; a book's
# ASIN is its ISBN-10, which may end in X). Anything else is not an ASIN.
_ASIN_FULL = re.compile(r"[A-Z0-9]{10}")

# An ASIN read out of an Amazon URL — anchored to Amazon's real product-page path forms (/dp/…,
# /gp/product/…) or a cart-add query param, and bounded on the right by "not another ASIN character"
# so an 11+-character token never half-matches while a trailing period, paren, comma, or
# percent-encoded query (a link pasted from an email or a web-search result) still reads cleanly.
# Deliberately NO bare /product/ alternative: that's another storefront's URL shape, and a 10-char
# path word there (verified: /product/categories) would mint a fabricated "ASIN".
_URL_ASIN = re.compile(
    r"(?:/dp/|/gp/product/|[?&]ASIN(?:\.\d+)?=)([A-Za-z0-9]{10})(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# The host a trusted product link must live on: amazon.<tld> (amazon.com, amazon.co.uk, amazon.de,
# amazon.com.au) with any subdomain (www., smile.). Full-match on the host, so evil-amazon.com,
# amazonx.com, and amazon.com@evil.com userinfo tricks all fail.
_AMAZON_HOST = re.compile(r"(?:[a-z0-9-]+\.)*amazon\.[a-z]{2,3}(?:\.[a-z]{2})?")


def is_amazon_host(host: str) -> bool:
    """True for an Amazon storefront host (amazon.com, smile.amazon.co.uk, …) — full-match, so
    lookalikes (amazonx.com, evil-amazon.com, amazon.com.evil.net) fail."""
    return bool(_AMAZON_HOST.fullmatch((host or "").strip().lower()))


def _amazon_hosted(text: str, start: int) -> bool:
    """True when the link fragment containing the match at `start` sits on an Amazon host — a
    /dp/-shaped path on some other site must never be read as an ASIN (never a guess)."""
    head = re.split(r"[\s()<>\"']+", text[:start])[-1]  # the URL the match lives in, up to the match
    if "://" in head:
        head = head.split("://", 1)[1]
    host = head.split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    return bool(_AMAZON_HOST.fullmatch(host))


@dataclass(frozen=True)
class CartItem:
    """One staged line: the user's words for the thing, the verified ASIN, how many — and the
    price as read from Amazon at staging time, so spoken money questions ('how much is it?',
    'what's the total so far?') answer from the staged list. An ESTIMATE only, never
    authoritative: Amazon's own cart page shows the live truth at checkout."""

    label: str     # plain words, e.g. "M3x8 socket screws (100 pack)"
    asin: str      # validated, uppercase
    quantity: int  # 1..MAX_QUANTITY
    price: float | None = None  # dollars per item as read when staged; None = no price was read
    title: str = ""    # the listing's own title, when HELIX read the page (what Amazon will add)
    image: str = ""    # the listing's picture (https://m.media-amazon.com/…) for the cart panel
    project: str = ""  # the parts list this line belongs to, if any
    note: str = ""     # "unverified: Amazon didn't answer" — honesty carried into the recap


def normalize_asin(text: str) -> str | None:
    """A bare ASIN, case-forgiven ('b08n5wrwnw' → 'B08N5WRWNW'), or None if it isn't one."""
    t = (text or "").strip().upper()
    return t if _ASIN_FULL.fullmatch(t) else None


def extract_asin(text: str) -> str | None:
    """The ASIN in `text` — a bare id, or an AMAZON product/cart link it can be read out of.
    Returns the uppercase ASIN, or None when nothing trustworthy is there (never a guess):
    the link must actually be Amazon-hosted, not merely Amazon-shaped."""
    bare = normalize_asin(text)
    if bare is not None:
        return bare
    t = (text or "").strip()
    m = _URL_ASIN.search(t)
    if m is None or not _amazon_hosted(t, m.start()):
        return None
    return m.group(1).upper()


def clamp_quantity(value) -> int:
    """A forgiving quantity read: numbers (or numeric strings) clamp to 1..MAX_QUANTITY; anything
    unreadable means 1 — the staged list is read back to the user before the cart ever opens.
    OverflowError covers the infinities: json parses 1e400 to float('inf'), and int(inf) raises."""
    try:
        q = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 1
    return max(1, min(q, MAX_QUANTITY))


def read_price(value) -> float | None:
    """A model-read price in dollars: accepts 12.99, "12.99", "$1,299.00". None when unreadable,
    non-positive, or absurd — a price here is an estimate for spoken answers, so refusing a bad
    read beats recording a wrong one. A STRING must look like US-format money (commas only as
    thousands groups): "12,99" and "1.299,00" are European renderings whose comma-stripped float
    would be confidently wrong by 100-1000x, and "1e3"/"1_299.00" are float-literal shapes no
    price tag prints — all refuse rather than guess. json's `true` is not a one-dollar price,
    and a sub-cent value rounds to $0.00 first so it can't sneak past the non-positive check."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        s = value.strip().lstrip("$").strip()
        if not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", s):
            return None
        value = s.replace(",", "")
    try:
        p = round(float(value), 2)
    except (TypeError, ValueError, OverflowError):  # OverflowError: float(10**400)
        return None
    if not (0 < p <= MAX_PRICE):  # non-finite floats fail here too, so inf never lands in a total
        return None
    return p


def price_summary(items: list[CartItem] | tuple[CartItem, ...]) -> str:
    """One plain estimated-total line for the staged list, or "" when no price was read. Honest
    about gaps: unpriced items are counted, never silently averaged in."""
    priced = [i for i in items if i.price is not None]
    if not priced:
        return ""
    total = sum(i.quantity * i.price for i in priced)
    missing = len(items) - len(priced)
    line = f"Estimated total about ${total:,.2f}"
    if missing:
        line += f" plus {missing} item{'s' if missing != 1 else ''} without a price read"
    return line + " (prices as read when staged; Amazon's cart page is the live truth)."


def cart_url(items: list[CartItem] | tuple[CartItem, ...]) -> str:
    """The one-click add-to-cart URL for every staged item, indexes 1..n."""
    params: list[tuple[str, str]] = []
    for i, item in enumerate(items, start=1):
        params.append((f"ASIN.{i}", item.asin))
        params.append((f"Quantity.{i}", str(item.quantity)))
    return f"{CART_BASE}?{urlencode(params)}"
