"""AmazonWeb — HELIX's own reads of amazon.com: a search page, a product page. Edge/I-O.

Plain HTTPS GETs with a browser's headers, nothing else: no cookies, no credentials, no session —
the same page an anonymous shopper sees, which carries exactly what the model needs (title, price,
stars, Prime, the ASIN). Egress is pinned to Amazon: only https, only GET, and a redirect is followed
only when it stays on an amazon.<tld> host (a stray 3xx elsewhere is refused). Nothing secret rides
in these requests, so there is nothing to scrub — but the posture matches call_api on principle.

Polite by construction: reads are paced (one every ~1.2s at most), cached for a few minutes (the
model iterates on the same search within a turn), and Amazon's automation wall is detected and
reported as "unavailable" rather than parsed as an empty page — the service then falls back to the
model's web search and says so, instead of pretending nothing matched.
"""
from __future__ import annotations

import gzip
import threading
import time
import urllib.error
import urllib.request
import zlib
from typing import Callable
from urllib.parse import quote_plus, urlsplit

from helix.domain.amazon import Listing, Product, is_blocked, parse_listing, parse_search
from helix.domain.shopping import is_amazon_host
from helix.logging_setup import get_logger

_LOG = get_logger("amazon")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/139.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
_TIMEOUT_S = 20.0
_MAX_BYTES = 6_000_000  # a search page is ~1.5 MB; a product page ~2 MB
_MIN_GAP_S = 1.2        # pacing between real fetches
_CACHE_TTL_S = 600.0    # a search repeated within a turn (or a quick "and the 3-pack?") costs nothing


class AmazonUnavailable(Exception):
    """Amazon didn't answer with a usable page (throttled, robot wall, network down)."""


class _AmazonOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it stays on an Amazon host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        host = (urlsplit(newurl).hostname or "").lower()
        if not is_amazon_host(host):
            _LOG.warning("refusing an off-Amazon redirect to %s", host)
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AmazonOnlyRedirects())


def _decode(raw: bytes, encoding: str) -> str:
    enc = (encoding or "").lower()
    try:
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        pass
    return raw.decode("utf-8", "replace")


def http_get(url: str) -> str:
    """One paced-by-the-caller GET, returning the page text. Raises AmazonUnavailable on transport
    trouble or a non-2xx that isn't a plain 404 (a 404 returns "" — a product that no longer exists
    is an answer, not an outage)."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with _OPENER.open(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read(_MAX_BYTES)
            return _decode(raw, resp.headers.get("Content-Encoding", ""))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ""
        raise AmazonUnavailable(f"Amazon answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AmazonUnavailable(f"couldn't reach Amazon ({exc.__class__.__name__})") from exc


class AmazonWeb:
    """Search + product reads, paced and cached. `fetch` is injectable for tests (url -> html)."""

    def __init__(self, *, fetch: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._fetch = fetch or http_get
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_fetch = 0.0
        self._cache: dict[str, tuple[float, str]] = {}

    # ----- pages -----
    def _get(self, url: str) -> str:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(url)
            if hit is not None and now - hit[0] < _CACHE_TTL_S:
                return hit[1]
            gap = _MIN_GAP_S - (now - self._last_fetch)
            if gap > 0:
                self._sleep(gap)
            self._last_fetch = self._clock()
            html = self._fetch(url)
            if is_blocked(html):
                raise AmazonUnavailable("Amazon is showing its robot check to automated reads right now")
            self._cache[url] = (self._clock(), html)
            if len(self._cache) > 64:  # a session's worth; oldest first
                for k in list(self._cache)[:16]:
                    self._cache.pop(k, None)
            return html

    def search_url(self, query: str) -> str:
        return f"https://www.amazon.com/s?k={quote_plus(' '.join(query.split()))}"

    def product_url(self, asin: str) -> str:
        return f"https://www.amazon.com/dp/{asin}?th=1&psc=1"

    # ----- reads -----
    def search(self, query: str, *, limit: int = 10) -> list[Product]:
        """The result cards for `query`. Raises AmazonUnavailable when Amazon won't answer."""
        q = " ".join((query or "").split())
        if not q:
            return []
        return parse_search(self._get(self.search_url(q)), limit=limit)

    def listing(self, asin: str) -> Listing | None:
        """The product page for `asin`, or None when Amazon has no such product page (404, or a
        page that isn't a listing). Raises AmazonUnavailable when Amazon won't answer."""
        html = self._get(self.product_url(asin))
        if not html:
            return None
        return parse_listing(html, asin)
