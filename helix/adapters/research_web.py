"""ResearchWeb — HELIX's own reads of the DOCUMENTED web: a search, a page, a datasheet. Edge/I-O.

The research faculty (READ_ME/DREAM_MIND.md §10) is how HELIX tells VERIFIED knowledge from what it
merely believes: a search runs through DuckDuckGo's keyless HTML endpoint, and a page is read only
when its host is on THE ALLOWLIST below — official documentation, code repositories, package
indexes, manufacturers, distributors, and a few community references. Everything else is refused
with one plain line naming the host. HELIX reads sources whose content it can trust as
documentation, never arbitrary pages: an autonomous night chews on what it reads, and a page that
could coach it must not be reachable at all.

The posture is amazon_web's, on principle and in code shape: plain HTTPS GETs with a browser's
headers, no cookies, no credentials, no POST — nothing secret ever rides in a research request, so
there is nothing to scrub. Redirects are followed only when they land back inside the allowlist (an
http downgrade or a hop to an off-list host is refused). Bodies are capped at 4 MB. Reads are paced
(one every 1.5 s at most) and cached for ten minutes, because a research turn iterates on the same
handful of pages. A PDF (a datasheet) goes through the injected `pdf_text` callable — the container
hands in services/doc_extract's text path; this adapter imports no service.

Parsing lives here too (the spec keeps the faculty in one adapter): the search page's result cards
with DuckDuckGo's `uddg` redirect parameter decoded to the real URL, and a page's readable text —
<main>, else <article>, else <body>, with script/style/nav/header/footer removed and whitespace
collapsed — capped with a plain "… (truncated)". lxml is imported lazily, as domain/amazon.py does,
so the module stays importable without it.
"""
from __future__ import annotations

import gzip
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlsplit

from helix.domain.shopping import is_amazon_host
from helix.logging_setup import get_logger

_LOG = get_logger("research")

# THE ALLOWLIST (DREAM_MIND.md §10): suffix-matched on the registrable host, subdomains included —
# wiki.seeedstudio.com and docs.espressif.com read; github.com.evil.net and evil-adafruit.com do not.
READ_HOSTS: tuple[str, ...] = (
    # code + packages (github.io / readme.io / gitbook.io host most projects' published docs)
    "github.com", "raw.githubusercontent.com", "gist.github.com", "github.io", "pypi.org", "npmjs.com",
    "readme.io", "gitbook.io",
    # the official docs of the services HELIX's own seeded watchers talk to (the first real night
    # had to verify SAM.gov, Alpaca and Slack facts from GitHub mirrors because these were refused)
    "gsa.gov", "alpaca.markets", "slack.com",
    "readthedocs.io", "readthedocs.org", "python.org", "developer.mozilla.org", "arxiv.org",
    # makers of the boards and parts on the bench
    "espressif.com", "arduino.cc", "seeedstudio.com", "adafruit.com", "sparkfun.com",
    "raspberrypi.com", "raspberrypi.org", "ti.com", "st.com", "microchip.com", "nordicsemi.com",
    "analog.com", "bosch-sensortec.com", "invensense.com", "tdk.com", "nxp.com", "infineon.com",
    "sensirion.com", "ams-osram.com", "omnivision.com",
    # distributors
    "digikey.com", "mouser.com",
    # printing and making
    "bambulab.com", "prusa3d.com", "thingiverse.com", "printables.com",
    # community references
    "hackaday.com", "hackaday.io", "instructables.com", "stackoverflow.com", "stackexchange.com",
    "reddit.com", "wikipedia.org",
)
# A comma-separated list of extra hosts the user may add (read live from settings on every call).
HOSTS_EXTRA_SETTING = "research_hosts_extra"
_MAX_EXTRA_HOSTS = 50
_HOST_SHAPE = re.compile(r"^(?!-)[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$")

# The search endpoint: DuckDuckGo's HTML face, keyless, a plain GET. Its redirects stay on
# duckduckgo.com; its result hosts are NOT readable through read() (duckduckgo.com is not on the list).
SEARCH_HOST = "html.duckduckgo.com"
_SEARCH_URL = "https://html.duckduckgo.com/html/?q="
_DDG_SUFFIX = "duckduckgo.com"
_SEARCH_WALL_MARKS = ("anomaly-modal", "bots use DuckDuckGo too")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/139.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
_TIMEOUT_S = 20.0
MAX_BYTES = 4_000_000        # the body cap (a datasheet PDF is ~1–3 MB; a docs page ~200 KB)
_MIN_GAP_S = 1.5             # pacing between real fetches
_SEARCH_GAP_S = 6.0          # searches pace slower: DuckDuckGo challenges bursts (both real nights hit its robot wall)
_CACHE_TTL_S = 600.0         # ten minutes — a research turn re-reads the same page freely
DEFAULT_MAX_CHARS = 12_000
TRUNCATED = "… (truncated)"
_TITLE_CHARS = 160
_SNIPPET_CHARS = 300

_DROP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "noscript", "template",
                        "svg", "iframe", "head", "object", "embed"})
_BLOCK_TAGS = frozenset({
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
    "article", "main", "pre", "blockquote", "dd", "dt", "dl", "figure", "figcaption", "hr", "form",
    "fieldset", "legend", "details", "summary", "address", "aside", "body", "html", "title",
})
_CELL_TAGS = frozenset({"td", "th"})
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)


class ResearchUnavailable(Exception):
    """The web didn't answer with a usable page (network down, an HTTP error, a search wall)."""


class ResearchRefused(Exception):
    """A read HELIX will not make — off the allowlist, not https, a userinfo trick, an unreadable
    PDF. The message is the one plain line the model is told, naming the host."""


@dataclass(frozen=True)
class Hit:
    """One search result. `readable` says whether read() would accept the host."""

    title: str
    url: str
    host: str
    snippet: str
    readable: bool


@dataclass(frozen=True)
class Page:
    """A page HELIX read: its text (capped) and where and when it was read."""

    url: str
    host: str
    title: str
    text: str
    fetched_at: str   # ISO "YYYY-MM-DDTHH:MM" — the source line's date comes from here


@dataclass(frozen=True)
class Fetched:
    """What one GET returned. `url` is the final URL after any (allowlisted) redirects; `complete`
    is False when the body hit the 4 MB cap."""

    url: str
    content_type: str
    body: bytes
    complete: bool = True


# ----- hosts -----
def host_of(url: str) -> str:
    """The lowercased hostname of `url` — no userinfo, no port, no trailing dot; "" when the
    address has no host or can't be parsed."""
    try:
        host = urlsplit((url or "").strip()).hostname or ""
    except ValueError:
        return ""
    return host.strip().rstrip(".").lower()


def host_allowed(host: str, hosts: tuple[str, ...] | list[str] = READ_HOSTS) -> bool:
    """Suffix match on the registrable host, subdomains included: 'wiki.seeedstudio.com' and
    'seeedstudio.com' pass for 'seeedstudio.com'; 'github.com.evil.net', 'evil-adafruit.com' and
    'xgithub.com' do not."""
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        return False
    return any(h == allowed or h.endswith("." + allowed) for allowed in hosts if allowed)


def parse_hosts_extra(text) -> tuple[str, ...]:
    """The user's extra hosts from the `research_hosts_extra` setting: comma- or space-separated,
    scheme and path tolerated ('https://docs.example.com/x' → 'docs.example.com'), lowercased,
    deduplicated, only host-shaped entries kept; amazon hosts never (the Amazon faculty owns them)."""
    if not isinstance(text, str):
        return ()
    out: list[str] = []
    for raw in re.split(r"[,\s]+", text):
        item = raw.strip().lower()
        if not item:
            continue
        if "://" in item:
            item = item.split("://", 1)[1]
        item = item.split("/", 1)[0].split("@")[-1].split(":")[0].rstrip(".")
        if item.startswith("www."):
            item = item[4:]
        if not _HOST_SHAPE.match(item) or is_amazon_host(item) or item.endswith(_DDG_SUFFIX):
            continue
        if item not in out:
            out.append(item)
        if len(out) >= _MAX_EXTRA_HOSTS:
            break
    return tuple(out)


def _search_redirect_ok(url: str) -> bool:
    """A search's redirect may only stay on DuckDuckGo, over https."""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return False
    host = host_of(url)
    return scheme == "https" and (host == _DDG_SUFFIX or host.endswith("." + _DDG_SUFFIX))


# ----- the search page -----
def decode_result_url(href: str) -> str:
    """The real URL behind a DuckDuckGo result link. The HTML endpoint wraps every hit as
    `//duckduckgo.com/l/?uddg=<percent-encoded url>&rut=…`; a plain absolute link is returned as
    is; anything else (an ad's y.js hop, a relative path) is "" — never a guess."""
    h = (href or "").strip()
    if h.startswith("//"):
        h = "https:" + h
    try:
        parts = urlsplit(h)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host == _DDG_SUFFIX or host.endswith("." + _DDG_SUFFIX):
        if parts.path.rstrip("/") == "/l":
            target = (parse_qs(parts.query).get("uddg") or [""])[0].strip()
            return target if target.lower().startswith(("http://", "https://")) else ""
        return ""
    if parts.scheme.lower() in ("http", "https") and host:
        return h
    return ""


def is_search_blocked(html: str) -> bool:
    """True when DuckDuckGo answered with its automation wall instead of results."""
    head = (html or "")[:300_000]
    return any(mark in head for mark in _SEARCH_WALL_MARKS)


def _parse_html(html: str):
    from lxml import html as lhtml  # lazy: importable without lxml, like domain/amazon.py

    text = _XML_DECL.sub("", html or "", count=1)  # lxml refuses str input with an XML declaration
    return lhtml.document_fromstring(text or "<html></html>")


def _node_text(node) -> str:
    try:
        return " ".join(node.text_content().split())
    except Exception:  # noqa: BLE001 — a comment node, a broken subtree
        return ""


def parse_search(html: str, *, limit: int = 8,
                 readable: Callable[[str], bool] | None = None) -> list[Hit]:
    """The organic result cards of a DuckDuckGo HTML results page, ads skipped, deduplicated by
    URL, `uddg` decoded. `readable(url)` decides the Hit's flag (None = every hit unreadable)."""
    if not html or is_search_blocked(html):
        return []
    doc = _parse_html(html)
    cards = doc.xpath('//div[contains(@class,"result") and contains(@class,"results_links")'
                      ' and not(contains(@class,"result--ad"))]')
    hits: list[Hit] = []
    seen: set[str] = set()
    for card in cards:
        links = card.xpath('.//a[contains(@class,"result__a")]')
        if not links:
            continue
        link = links[0]
        url = decode_result_url(link.get("href") or "")
        title = _node_text(link)[:_TITLE_CHARS]
        if not url or not title or url in seen:
            continue
        host = host_of(url)
        if not host:
            continue
        seen.add(url)
        snips = card.xpath('.//*[contains(@class,"result__snippet")]')
        snippet = _node_text(snips[0])[:_SNIPPET_CHARS] if snips else ""
        ok = bool(readable(url)) if readable is not None else False
        hits.append(Hit(title=title, url=url, host=host, snippet=snippet, readable=ok))
        if len(hits) >= max(1, limit):
            break
    return hits


# ----- page text -----
def _collect_text(root) -> str:
    """The visible text under `root`, dropped subtrees left out, a newline after each block-level
    element so paragraphs survive the whitespace collapse. Iterative (a deep page never recurses)."""
    parts: list[str] = []
    stack: list[tuple[str, object]] = [("enter", root)]
    while stack:
        kind, node = stack.pop()
        if kind == "text":
            parts.append(str(node))
            continue
        tag = node.tag.lower() if isinstance(node.tag, str) else ""  # comments carry a callable tag
        if kind == "exit":
            if tag in _BLOCK_TAGS:
                parts.append("\n")
            elif tag in _CELL_TAGS:
                parts.append(" ")
            continue
        if tag in _DROP_TAGS:
            continue
        if tag == "br":
            parts.append("\n")
        if node.text:
            parts.append(node.text)
        stack.append(("exit", node))
        for child in reversed(list(node)):
            if child.tail:
                stack.append(("text", child.tail))
            stack.append(("enter", child))
    return "".join(parts)


def collapse_text(text: str) -> str:
    """Whitespace collapsed: runs of spaces to one, blank lines dropped, one line per block."""
    lines = (" ".join(line.split()) for line in (text or "").split("\n"))
    return "\n".join(line for line in lines if line)


def cap_text(text: str, max_chars: int) -> str:
    """Cap at `max_chars` with a plain truncation mark (the model is told, never left guessing)."""
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip() + " " + TRUNCATED
    return text


def extract_text(html: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, str]:
    """(title, text) of an HTML page: <main>, else <article>, else <body>, with script/style/
    nav/header/footer removed, whitespace collapsed, capped."""
    doc = _parse_html(html)
    title = ""
    for xp in ("//title", "//h1"):
        nodes = doc.xpath(xp)
        if nodes:
            title = _node_text(nodes[0])[:_TITLE_CHARS]
            if title:
                break
    root = doc
    for xp in ("//main", "//article", "//body"):
        nodes = doc.xpath(xp)
        if nodes:
            root = nodes[0]
            break
    return title, cap_text(collapse_text(_collect_text(root)), max_chars)


def _charset(content_type: str, body: bytes) -> str:
    m = re.search(r"charset=[\"']?\s*([A-Za-z0-9_.:-]+)", content_type or "", re.IGNORECASE)
    if m:
        return m.group(1)
    m = _META_CHARSET.search(body[:4096])
    return m.group(1).decode("ascii", "ignore") if m else "utf-8"


def decode_body(fetched: Fetched) -> str:
    """The body as text, by the declared charset (header, then <meta>), utf-8 with replacement
    when the declaration is missing or wrong."""
    enc = _charset(fetched.content_type, fetched.body)
    try:
        return fetched.body.decode(enc, "replace")
    except LookupError:
        return fetched.body.decode("utf-8", "replace")


def looks_like_pdf(fetched: Fetched) -> bool:
    return "application/pdf" in (fetched.content_type or "").lower() or fetched.body[:5] == b"%PDF-"


def _looks_like_html(fetched: Fetched) -> bool:
    ctype = (fetched.content_type or "").lower()
    if "html" in ctype or "xml" in ctype:
        return True
    if ctype.startswith("text/") or "json" in ctype:
        return False
    return fetched.body.lstrip()[:1] == b"<"


# ----- transport -----
class _AllowlistRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when `allowed(newurl)` says the target is inside the leash — for a
    read, the allowlist over https; for a search, duckduckgo.com over https. A refused hop is
    remembered so the caller can say plainly where the page tried to send it."""

    def __init__(self, allowed: Callable[[str], bool]) -> None:
        super().__init__()
        self._allowed = allowed
        self.refused: str | None = None

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if not self._allowed(newurl):
            self.refused = newurl
            _LOG.warning("refusing a redirect to %s", host_of(newurl) or newurl[:60])
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _inflate(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return zlib.decompress(raw)
    except (OSError, EOFError, zlib.error):
        pass
    return raw


def http_get(url: str, *, allowed: Callable[[str], bool]) -> Fetched:
    """One paced-by-the-caller GET: a browser's headers, no cookies, redirects only where `allowed`
    says, the body capped at MAX_BYTES. Raises ResearchRefused when a redirect was refused, else
    ResearchUnavailable on transport trouble or a non-2xx."""
    handler = _AllowlistRedirects(allowed)
    opener = urllib.request.build_opener(handler)   # no cookie processor: nothing is ever kept
    req = urllib.request.Request(url, headers=_HEADERS)  # GET — the only verb this module has
    host = host_of(url) or "the site"
    try:
        with opener.open(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read(MAX_BYTES + 1)
            complete = len(raw) <= MAX_BYTES
            encoding = resp.headers.get("Content-Encoding", "")
            if not complete and encoding:
                raise ResearchUnavailable(f"{host}'s page is larger than 4 MB")
            body = _inflate(raw[:MAX_BYTES], encoding)
            return Fetched(url=resp.geturl(), content_type=resp.headers.get("Content-Type", "") or "",
                           body=body, complete=complete)
    except urllib.error.HTTPError as exc:
        if handler.refused:
            raise ResearchRefused(
                f"{host} redirected to {host_of(handler.refused) or 'another address'}, which "
                "isn't on HELIX's reading list — not followed."
            ) from exc
        raise ResearchUnavailable(f"{host} answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if handler.refused:
            raise ResearchRefused(
                f"{host} redirected to {host_of(handler.refused) or 'another address'}, which "
                "isn't on HELIX's reading list — not followed."
            ) from exc
        raise ResearchUnavailable(f"couldn't reach {host} ({exc.__class__.__name__})") from exc


class ResearchWeb:
    """Search + page reads on the leash: paced, cached, allowlisted. `fetch(url, allowed=…)` is
    injectable for tests (→ Fetched); `settings.get` supplies the user's extra hosts live;
    `pdf_text(path)` turns a downloaded PDF into text (the container hands in doc_extract.extract —
    an adapter imports no service); `clock` is monotonic seconds, `wall` the date stamp."""

    def __init__(self, *, fetch: Callable[..., Fetched] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 wall: Callable[[], datetime] | None = None,
                 settings=None,
                 pdf_text: Callable[[Path], str] | None = None) -> None:
        self._fetch = fetch or http_get
        self._clock = clock
        self._sleep = sleep
        self._wall = wall or datetime.now
        self._settings = settings
        self._pdf_text = pdf_text
        self._lock = threading.Lock()
        self._last_fetch = 0.0
        self._cache: dict[str, tuple[float, Fetched]] = {}

    # ----- the allowlist -----
    def extra_hosts(self) -> tuple[str, ...]:
        if self._settings is None:
            return ()
        try:
            return parse_hosts_extra(self._settings.get(HOSTS_EXTRA_SETTING))
        except Exception:  # noqa: BLE001 — a store hiccup must never widen or crash a read
            return ()

    def hosts(self) -> tuple[str, ...]:
        """The reading list: READ_HOSTS plus the user's extras (read live)."""
        return READ_HOSTS + tuple(h for h in self.extra_hosts() if h not in READ_HOSTS)

    def host_allowed(self, host: str) -> bool:
        return host_allowed(host, self.hosts())

    def refusal(self, url: str) -> str | None:
        """The one plain line read() would refuse `url` with, or None when it would read it."""
        u = (url or "").strip()
        if not u:
            return "That isn't a web address I can read."
        try:
            parts = urlsplit(u)
        except ValueError:
            return "That isn't a web address I can read."
        scheme = parts.scheme.lower()
        host = host_of(u)
        if scheme != "https":
            if scheme == "http":
                return f"I read https pages only — {host or u} was given over plain http."
            return f"I read https pages only, not {scheme + ' addresses' if scheme else 'that'}."
        if not host:
            return "That isn't a web address I can read."
        if "@" in parts.netloc:
            return f"I don't read addresses that carry a sign-in name ({parts.netloc})."
        try:
            port = parts.port
        except ValueError:
            port = -1
        if port is not None:
            return f"I don't read addresses with a port number ({parts.netloc})."
        if is_amazon_host(host):
            return ("amazon.com is read through search_amazon and lookup_amazon, not as a raw "
                    "page.")
        if not self.host_allowed(host):
            return (f"I don't read {host} — HELIX reads only sources it can trust as documentation "
                    "(official docs, code repositories, package indexes, manufacturers, "
                    "distributors, and a few references such as Wikipedia and Stack Overflow); a "
                    f"host can be added under the setting {HOSTS_EXTRA_SETTING}.")
        return None

    def readable(self, url: str) -> bool:
        return self.refusal(url) is None

    # ----- pages -----
    def _get(self, url: str, allowed: Callable[[str], bool]) -> Fetched:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(url)
            if hit is not None and now - hit[0] < _CACHE_TTL_S:
                return hit[1]
            pace = _SEARCH_GAP_S if url.startswith(_SEARCH_URL) else _MIN_GAP_S
            gap = pace - (now - self._last_fetch)
            if gap > 0:
                self._sleep(gap)
            self._last_fetch = self._clock()
            fetched = self._fetch(url, allowed=allowed)
            self._cache[url] = (self._clock(), fetched)
            if len(self._cache) > 64:  # a night's worth; oldest first
                for k in list(self._cache)[:16]:
                    self._cache.pop(k, None)
            return fetched

    def search_url(self, query: str) -> str:
        return _SEARCH_URL + quote_plus(" ".join((query or "").split()))

    # ----- reads -----
    def search(self, query: str, *, max_results: int = 8) -> list[Hit]:
        """The organic hits for `query`, readable-flagged. Raises ResearchUnavailable when the
        search didn't answer (network, an HTTP error, DuckDuckGo's automation wall)."""
        q = " ".join((query or "").split())
        if not q:
            return []
        _LOG.info("research search: %s", q)  # the query text is journaled, never a secret
        fetched = self._get(self.search_url(q), _search_redirect_ok)
        html = decode_body(fetched)
        if is_search_blocked(html):
            raise ResearchUnavailable("DuckDuckGo is challenging automated searches right now")
        hits = parse_search(html, limit=max_results, readable=self.readable)
        _LOG.info("research search: %d hit(s) for %s", len(hits), q)
        return hits

    def read(self, url: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> Page:
        """The page at `url` as capped text. Raises ResearchRefused (off the allowlist, not https,
        a userinfo/port trick, a redirect off the list, a PDF that can't be read) or
        ResearchUnavailable (the host didn't answer)."""
        why = self.refusal(url)
        if why:
            raise ResearchRefused(why)
        u = url.strip()
        fetched = self._get(u, self.readable)
        final_url = fetched.url or u
        host = host_of(final_url) or host_of(u)
        if not self.readable(final_url):  # belt to the redirect handler's brace
            raise ResearchRefused(f"{host} isn't on HELIX's reading list — not read.")
        stamp = self._wall().strftime("%Y-%m-%dT%H:%M")
        _LOG.info("research read: %s", final_url)
        if looks_like_pdf(fetched):
            text = self._pdf(fetched, host)
            title = Path(urlsplit(final_url).path).name or host
            return Page(url=final_url, host=host, title=title[:_TITLE_CHARS],
                        text=cap_text(text, max_chars), fetched_at=stamp)
        if _looks_like_html(fetched):
            title, text = extract_text(decode_body(fetched), max_chars=max_chars)
        else:
            title = Path(urlsplit(final_url).path).name or host
            text = cap_text(collapse_text(decode_body(fetched)), max_chars)
        return Page(url=final_url, host=host, title=title, text=text, fetched_at=stamp)

    def _pdf(self, fetched: Fetched, host: str) -> str:
        if self._pdf_text is None:
            raise ResearchRefused(f"{host} answered with a PDF, and PDF reading isn't wired on this "
                                  "build — read the HTML page for it instead.")
        if not fetched.complete:
            raise ResearchRefused(f"The PDF at {host} is larger than 4 MB — too big to read here.")
        fd, name = tempfile.mkstemp(prefix="helix-research-", suffix=".pdf")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(fetched.body)
            text = collapse_text(str(self._pdf_text(path) or ""))
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if not text:
            raise ResearchRefused(f"The PDF at {host} has no readable text (a scan without a text "
                                  "layer, or an encrypted file).")
        return text
