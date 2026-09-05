"""The research faculty (READ_ME/DREAM_MIND.md §10): the allowlisted, paced, cached fetcher and its
search/page parsers (adapters/research_web.py), the model-facing text (services/research.py), and
the five tools through the ToolRegistry — the leash holds (lookalikes, userinfo/port tricks, http,
off-list redirects), a fact can only be noted from a page HELIX read itself, and readable text
never coaches a fenced tool."""
from __future__ import annotations

import io
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from helix.adapters import research_web as rw
from helix.adapters.research_web import (
    MAX_BYTES,
    READ_HOSTS,
    TRUNCATED,
    Fetched,
    ResearchRefused,
    ResearchUnavailable,
    ResearchWeb,
    _AllowlistRedirects,
    decode_result_url,
    extract_text,
    host_allowed,
    host_of,
    http_get,
    parse_hosts_extra,
    parse_search,
)
from helix.adapters.json_settings import JsonSettings
from helix.domain.vocabulary import friendly_tool_label
from helix.services.conversation import BUILD_TOOLS, DREAM_WRITES
from helix.services.research import ResearchService, find_passages
from helix.services.tools import ToolRegistry
from helix.services.verified import VerifiedStore

RESEARCH_TOOLS = ("research_search", "research_read", "verified_facts", "note_verified_fact",
                  "forget_verified")

# ----- fixtures: a DuckDuckGo HTML results page in its saved shape, and documentation pages -----


def _ddg_link(url: str) -> str:
    from urllib.parse import quote

    return f"//duckduckgo.com/l/?uddg={quote(url, safe='')}&amp;rut=1a2b3c"


def _result(title: str, url: str, snippet: str, *, ad: bool = False, direct: bool = False) -> str:
    href = url if direct else _ddg_link(url)
    cls = "result results_links results_links_deep web-result " + ("result--ad" if ad else "")
    return f"""
    <div class="{cls}">
      <div class="links_main links_deep result__body">
        <h2 class="result__title">
          <a rel="nofollow" class="result__a" href="{href}">{title}</a>
        </h2>
        <div class="result__extras">
          <div class="result__extras__url">
            <span class="result__icon"><a rel="nofollow" href="{href}">
              <img class="result__icon__img" width="16" height="16" alt="" src="//external-content.duckduckgo.com/ip3/x.ico"></a></span>
            <a class="result__url" href="{href}">{url.split('://', 1)[1][:60]}</a>
          </div>
        </div>
        <a class="result__snippet" href="{href}">{snippet}</a>
        <div class="clear"></div>
      </div>
    </div>"""


SEEED_URL = "https://wiki.seeedstudio.com/xiao_esp32s3_camera_usage/"
AMAZON_URL = "https://www.amazon.com/Seeed-Studio-XIAO-ESP32S3-Sense/dp/B0C69FFVHH"
BLOG_URL = "https://random-maker-blog.example/esp32s3-camera"
HTTP_URL = "http://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/"
GITHUB_URL = "https://github.com/espressif/esp32-camera"
SEARCH_URL = "https://html.duckduckgo.com/html/?q=xiao+esp32s3+sense+camera"

DDG_HTML = f"""<!DOCTYPE html><html lang="en-US" class="no-js"><head><title>xiao esp32s3 sense camera at DuckDuckGo</title></head>
<body class="body--html"><div><div id="header" class="header cw header--html">
<form action="/html/" method="post"><input type="text" name="q" value="xiao esp32s3 sense camera"></form></div>
<div id="links_wrapper" class="serp__results"><div class="results--main"><div id="links" class="results">
{_result("Sponsored: Cheap ESP32 boards", "https://duckduckgo.com/y.js?ad_domain=shop.example&amp;u3=https%3A%2F%2Fshop.example", "Buy now", ad=True)}
{_result("Camera Usage in Seeed Studio XIAO ESP32S3 Sense", SEEED_URL, "The <b>XIAO ESP32S3 Sense</b> integrates an OV2640 camera sensor and 8MB PSRAM.")}
{_result("Seeed Studio XIAO ESP32S3 Sense : Amazon", AMAZON_URL, "2.4GHz Wi-Fi, BLE, OV2640 camera, USB-C")}
{_result("My ESP32-S3 camera build", BLOG_URL, "I wired the camera ribbon and it worked on the third try.")}
{_result("ESP32-S3 documentation", HTTP_URL, "ESP-IDF programming guide.")}
{_result("GitHub - espressif/esp32-camera", GITHUB_URL, "ESP32 camera driver", direct=True)}
{_result("Camera Usage (duplicate card)", SEEED_URL, "the same page again")}
<div class="nav-link"><form action="/html/" method="post"><input type="submit" class="btn btn--alt" value="Next"></form></div>
</div></div></div></div></body></html>"""

WALL_HTML = """<html><body><div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
<div class="anomaly-modal__description">Please complete the following challenge to confirm this search was made by a human.</div></body></html>"""

DOC_HTML = """<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>
<html><head><title>Camera Usage | Seeed Studio Wiki</title><style>.x{color:red}</style>
<script>window.dataLayer = ["TRACKING-SCRIPT-TEXT"];</script></head>
<body>
<header><a href="/">SITE-HEADER-LOGO</a></header>
<nav><ul><li>SIDEBAR-NAV-LINK-ONE</li><li>SIDEBAR-NAV-LINK-TWO</li></ul></nav>
<main>
  <h1>Camera Usage in Seeed Studio XIAO ESP32S3 Sense</h1>
  <p>The XIAO ESP32S3 Sense integrates an OV2640 camera sensor, a digital microphone and SD card
  support.</p>
  <script>console.log("INLINE-SCRIPT-IN-MAIN")</script>
  <h2>Hardware</h2>
  <table><tr><th>Item</th><th>Value</th></tr><tr><td>PSRAM</td><td>8MB PSRAM</td></tr>
  <tr><td>Flash</td><td>8MB Flash</td></tr></table>
  <ul><li>OV2640 camera</li><li>Xtensa LX7 dual-core</li></ul>
  <p>Connect the camera board to the B2B connector on the XIAO ESP32S3.</p>
</main>
<footer>SITE-FOOTER-COPYRIGHT</footer>
<script>track("FOOTER-SCRIPT")</script>
</body></html>"""


def _fetched(url: str, body: str | bytes, ctype: str = "text/html; charset=utf-8",
             complete: bool = True) -> Fetched:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return Fetched(url=url, content_type=ctype, body=raw, complete=complete)


class _Mono:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Settings:
    def __init__(self, **kv) -> None:
        self.d = dict(kv)

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value) -> None:
        self.d[key] = value


def _web(pages: dict[str, Fetched], *, settings=None, pdf_text=None):
    """A ResearchWeb over a fake fetch: records every URL fetched and every pacing sleep."""
    calls: list[str] = []
    slept: list[float] = []
    clock = _Mono()

    def fetch(url: str, *, allowed) -> Fetched:
        calls.append(url)
        if url not in pages:
            raise ResearchUnavailable(f"{host_of(url)} answered HTTP 404")
        return pages[url]

    def sleep(s: float) -> None:
        slept.append(s)
        clock.t += s

    web = ResearchWeb(fetch=fetch, clock=clock, sleep=sleep,
                      wall=lambda: datetime(2026, 9, 4, 23, 12), settings=settings,
                      pdf_text=pdf_text)
    return web, calls, slept, clock


# ----- hosts and the allowlist -----

def test_host_of_reads_the_real_host_and_never_the_decoy():
    assert host_of("https://Wiki.SeeedStudio.com./x") == "wiki.seeedstudio.com"
    assert host_of("https://github.com@evil.net/x") == "evil.net"     # the userinfo trick, seen through
    assert host_of("https://github.com:8443/x") == "github.com"
    assert host_of("not a url") == ""
    assert host_of("") == ""


def test_the_allowlist_suffix_matches_registrable_hosts_with_subdomains():
    assert host_allowed("github.com")
    assert host_allowed("wiki.seeedstudio.com")
    assert host_allowed("docs.espressif.com")
    assert host_allowed("SEEEDSTUDIO.COM.")
    assert not host_allowed("github.com.evil.net")
    assert not host_allowed("evil-adafruit.com")
    assert not host_allowed("xgithub.com")
    assert not host_allowed("")
    assert host_allowed("docs.example.com", ("example.com",))
    for spec_host in ("espressif.com", "digikey.com", "mouser.com", "wikipedia.org", "reddit.com",
                      "bambulab.com", "sensirion.com", "arxiv.org", "readthedocs.io"):
        assert spec_host in READ_HOSTS, spec_host


@pytest.mark.parametrize("url, fragment", [
    ("https://github.com.evil.net/repo", "I don't read github.com.evil.net"),
    ("https://evil-adafruit.com/learn", "I don't read evil-adafruit.com"),
    ("https://xgithub.com/x", "I don't read xgithub.com"),
    ("https://github.com@evil.net/x", "sign-in name (github.com@evil.net)"),
    ("https://evil.net@github.com/x", "sign-in name (evil.net@github.com)"),
    ("http://github.com/x", "https pages only — github.com was given over plain http"),
    ("ftp://github.com/x", "https pages only, not ftp addresses"),
    ("https://github.com:8443/x", "port number (github.com:8443)"),
    ("https://www.amazon.com/dp/B0C69FFVHH", "search_amazon and lookup_amazon"),
    ("https://html.duckduckgo.com/html/?q=x", "I don't read html.duckduckgo.com"),
    ("", "isn't a web address"),
    ("https:///nohost", "isn't a web address"),
])
def test_the_leash_refuses_with_one_plain_line_naming_the_host(url, fragment):
    web, calls, _, _ = _web({})
    why = web.refusal(url)
    assert why is not None and fragment in why, why
    assert not web.readable(url)
    with pytest.raises(ResearchRefused) as exc:
        web.read(url)
    assert fragment in str(exc.value)
    assert calls == []   # refused BEFORE any fetch


def test_the_off_list_refusal_says_why_the_allowlist_exists():
    web, _, _, _ = _web({})
    why = web.refusal("https://random-maker-blog.example/post")
    assert "random-maker-blog.example" in why
    assert "documentation" in why and "research_hosts_extra" in why


def test_allowlisted_https_pages_are_readable():
    web, _, _, _ = _web({})
    for url in (SEEED_URL, GITHUB_URL, "https://raw.githubusercontent.com/a/b/main/README.md",
                "https://docs.espressif.com/projects/esp-idf/", "https://www.digikey.com/en/products",
                "https://en.wikipedia.org/wiki/ESP32", "https://pypi.org/project/lxml/"):
        assert web.refusal(url) is None, url
        assert web.readable(url)


def test_extra_hosts_from_settings_widen_the_list_live_but_never_amazon():
    settings = _Settings()
    web, _, _, _ = _web({}, settings=settings)
    assert not web.readable("https://docs.example.com/x")
    settings.set("research_hosts_extra",
                 "docs.example.com, https://Wiki.Example.org/path?x=1 www.foo.com amazon.com junk "
                 "html.duckduckgo.com evil@user.com")
    assert web.readable("https://docs.example.com/x")
    assert web.readable("https://sub.docs.example.com/x")     # subdomains of an extra host too
    assert web.readable("https://wiki.example.org/y")
    assert web.readable("https://foo.com/z")
    assert not web.readable("https://www.amazon.com/dp/B0C69FFVHH")   # the Amazon faculty's
    assert not web.readable("https://junk/")
    assert "html.duckduckgo.com" not in web.hosts()
    assert web.hosts()[: len(READ_HOSTS)] == READ_HOSTS


def test_parse_hosts_extra_shapes():
    assert parse_hosts_extra("a.com, b.com") == ("a.com", "b.com")
    assert parse_hosts_extra("https://docs.x.io/p, www.y.org., y.org") == ("docs.x.io", "y.org")
    assert parse_hosts_extra(None) == () and parse_hosts_extra(42) == () and parse_hosts_extra("") == ()
    assert parse_hosts_extra("-bad.com, good-one.co.uk") == ("good-one.co.uk",)


# ----- redirects -----

def test_redirects_are_followed_only_within_the_allowlist_over_https():
    web, _, _, _ = _web({})
    h = _AllowlistRedirects(web.readable)
    req = urllib.request.Request(GITHUB_URL)
    assert h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/x") is None
    assert h.refused == "https://evil.example/x"
    assert h.redirect_request(req, None, 302, "Found", {}, "https://github.com.evil.net/x") is None
    assert h.redirect_request(req, None, 302, "Found", {}, "http://github.com/x") is None  # no downgrade
    ok = h.redirect_request(req, None, 302, "Found", {},
                            "https://raw.githubusercontent.com/espressif/esp32-camera/master/README.md")
    assert ok is not None and ok.full_url.startswith("https://raw.githubusercontent.com/")
    assert ok.get_method() == "GET"


def test_a_search_redirect_may_only_stay_on_duckduckgo():
    h = _AllowlistRedirects(rw._search_redirect_ok)
    req = urllib.request.Request("https://html.duckduckgo.com/html/?q=x")
    assert h.redirect_request(req, None, 302, "Found", {}, "https://duckduckgo.com/html/?q=x") is not None
    assert h.redirect_request(req, None, 302, "Found", {}, "http://duckduckgo.com/html/?q=x") is None
    assert h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/?q=x") is None


class _Resp:
    """A minimal urllib response: headers, a capped read, the final URL."""

    def __init__(self, body: bytes, headers: dict, url: str) -> None:
        self._body = io.BytesIO(body)
        self.headers = headers
        self._url = url

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass


class _Opener:
    def __init__(self, outcome, handler) -> None:
        self._outcome = outcome
        self.handler = handler
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        out = self._outcome
        if callable(out):
            out = out(self.handler)
        if isinstance(out, Exception):
            raise out
        return out


def _patch_opener(monkeypatch, outcome) -> list:
    made: list[_Opener] = []

    def build_opener(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], _AllowlistRedirects)  # no cookie jar
        op = _Opener(outcome, handlers[0])
        made.append(op)
        return op

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    return made


def test_http_get_is_a_plain_get_with_browser_headers_and_no_cookie_or_secret(monkeypatch):
    made = _patch_opener(monkeypatch, _Resp(b"<html><body>hi</body></html>",
                                            {"Content-Type": "text/html"}, GITHUB_URL))
    out = http_get(GITHUB_URL, allowed=lambda u: True)
    req = made[0].requests[0]
    assert req.get_method() == "GET"
    sent = {k.lower() for k in req.headers}
    assert "cookie" not in sent and "authorization" not in sent
    assert req.get_header("User-agent", "").startswith("Mozilla/5.0")
    assert out.body == b"<html><body>hi</body></html>" and out.complete and out.url == GITHUB_URL


def test_http_get_caps_the_body_at_four_megabytes(monkeypatch):
    _patch_opener(monkeypatch, _Resp(b"x" * (MAX_BYTES + 10), {"Content-Type": "text/plain"}, GITHUB_URL))
    out = http_get(GITHUB_URL, allowed=lambda u: True)
    assert len(out.body) == MAX_BYTES and not out.complete
    # a compressed body cut at the cap can't be inflated honestly → unavailable, not garbage
    _patch_opener(monkeypatch, _Resp(b"x" * (MAX_BYTES + 10),
                                     {"Content-Type": "text/html", "Content-Encoding": "gzip"}, GITHUB_URL))
    with pytest.raises(ResearchUnavailable) as exc:
        http_get(GITHUB_URL, allowed=lambda u: True)
    assert "larger than 4 MB" in str(exc.value)


def test_http_get_inflates_gzip(monkeypatch):
    import gzip

    _patch_opener(monkeypatch, _Resp(gzip.compress(b"<p>zipped</p>"),
                                     {"Content-Type": "text/html", "Content-Encoding": "gzip"}, GITHUB_URL))
    assert http_get(GITHUB_URL, allowed=lambda u: True).body == b"<p>zipped</p>"


def test_a_refused_redirect_is_a_plain_refusal_naming_both_hosts(monkeypatch):
    def bounce(handler):
        handler.redirect_request(urllib.request.Request(GITHUB_URL), None, 302, "Found", {},
                                 "https://evil.example/landing")
        return urllib.error.HTTPError(GITHUB_URL, 302, "Found", {}, None)

    _patch_opener(monkeypatch, bounce)
    with pytest.raises(ResearchRefused) as exc:
        http_get(GITHUB_URL, allowed=lambda u: host_of(u) == "github.com")
    assert "github.com redirected to evil.example" in str(exc.value)


def test_http_errors_and_network_trouble_are_unavailable(monkeypatch):
    _patch_opener(monkeypatch, urllib.error.HTTPError(GITHUB_URL, 404, "Not Found", {}, None))
    with pytest.raises(ResearchUnavailable) as exc:
        http_get(GITHUB_URL, allowed=lambda u: True)
    assert "github.com answered HTTP 404" in str(exc.value)
    _patch_opener(monkeypatch, urllib.error.URLError("dns down"))
    with pytest.raises(ResearchUnavailable) as exc:
        http_get(GITHUB_URL, allowed=lambda u: True)
    assert "couldn't reach github.com" in str(exc.value)


# ----- the search page -----

def test_uddg_decoding_shapes():
    assert decode_result_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fwiki.seeedstudio.com%2Fx&rut=abc") \
        == "https://wiki.seeedstudio.com/x"
    assert decode_result_url("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fa%2Fb") \
        == "https://github.com/a/b"
    assert decode_result_url("https://example.com/p?q=1") == "https://example.com/p?q=1"   # direct link
    assert decode_result_url("//duckduckgo.com/y.js?ad_domain=shop.example&u3=x") == ""   # an ad hop
    assert decode_result_url("/html/?q=next") == ""                                       # relative
    assert decode_result_url("//duckduckgo.com/l/?uddg=javascript%3Aalert(1)") == ""      # not a web URL
    assert decode_result_url("") == ""


def test_search_page_parses_cards_skips_ads_dedupes_and_flags_readability():
    web, calls, _, _ = _web({SEARCH_URL: _fetched(SEARCH_URL, DDG_HTML)})
    hits = web.search("  xiao   esp32s3 sense camera ")
    urls = [h.url for h in hits]
    assert urls == [SEEED_URL, AMAZON_URL, BLOG_URL, HTTP_URL, GITHUB_URL]   # ad out, duplicate out
    seeed = hits[0]
    assert seeed.title == "Camera Usage in Seeed Studio XIAO ESP32S3 Sense"
    assert seeed.host == "wiki.seeedstudio.com" and seeed.readable
    assert "OV2640 camera sensor and 8MB PSRAM" in seeed.snippet   # the <b> markup is flattened
    by_host = {h.host: h for h in hits}
    assert not by_host["www.amazon.com"].readable          # the Amazon faculty's, not a raw read
    assert not by_host["random-maker-blog.example"].readable
    assert not by_host["docs.espressif.com"].readable      # http:// — read() takes https only
    assert by_host["github.com"].readable                  # a direct (unwrapped) href
    assert calls == [SEARCH_URL]


def test_search_limit_empty_and_wall():
    url = "https://html.duckduckgo.com/html/?q=x"
    web, calls, _, _ = _web({url: _fetched(url, DDG_HTML)})
    assert len(web.search("x", max_results=2)) == 2
    assert web.search("   ") == [] and calls == [url]
    assert parse_search("") == [] and parse_search("<html><body>No results.</body></html>") == []
    walled, _, _, _ = _web({url: _fetched(url, WALL_HTML)})
    with pytest.raises(ResearchUnavailable) as exc:
        walled.search("x")
    assert "DuckDuckGo" in str(exc.value)


# ----- page text -----

def test_read_extracts_main_text_and_drops_nav_script_style_header_footer():
    web, _, _, _ = _web({SEEED_URL: _fetched(SEEED_URL, DOC_HTML)})
    page = web.read(SEEED_URL)
    assert page.url == SEEED_URL and page.host == "wiki.seeedstudio.com"
    assert page.title == "Camera Usage | Seeed Studio Wiki"
    assert page.fetched_at == "2026-09-04T23:12"
    for kept in ("Camera Usage in Seeed Studio XIAO ESP32S3 Sense", "OV2640 camera sensor",
                 "PSRAM 8MB PSRAM", "Xtensa LX7 dual-core", "B2B connector"):
        assert kept in page.text, kept
    for dropped in ("SITE-HEADER-LOGO", "SIDEBAR-NAV-LINK", "SITE-FOOTER-COPYRIGHT", "TRACKING-SCRIPT",
                    "INLINE-SCRIPT-IN-MAIN", "FOOTER-SCRIPT", "color:red", "<p>", "<td>"):
        assert dropped not in page.text, dropped
    # whitespace collapsed: one line per block, no runs of spaces, no blank lines
    assert "  " not in page.text and "\n\n" not in page.text
    assert page.text.startswith("Camera Usage in Seeed Studio XIAO ESP32S3 Sense\n")


def test_extract_text_falls_back_to_article_then_body():
    title, text = extract_text("<html><body><nav>NAV</nav><article><p>Article body.</p></article>"
                               "<p>outside</p></body></html>")
    assert text == "Article body." and title == ""
    title, text = extract_text("<html><head><title>T</title></head><body><header>H</header>"
                               "<p>Body only.</p><footer>F</footer></body></html>")
    assert text == "Body only." and title == "T"
    assert extract_text("<html><body><h1>Head</h1></body></html>")[0] == "Head"


def test_read_truncates_at_max_chars_with_the_mark():
    long_html = "<html><body><main>" + "".join(f"<p>paragraph {i} of the page</p>" for i in range(400)) \
        + "</main></body></html>"
    web, _, _, _ = _web({GITHUB_URL: _fetched(GITHUB_URL, long_html)})
    page = web.read(GITHUB_URL, max_chars=500)
    assert page.text.endswith(TRUNCATED) and len(page.text) <= 500 + len(TRUNCATED) + 1
    assert web.read(GITHUB_URL, max_chars=100_000).text.endswith("paragraph 399 of the page")


def test_plain_text_and_json_bodies_are_read_as_text():
    raw = "https://raw.githubusercontent.com/espressif/esp32-camera/master/README.md"
    web, _, _, _ = _web({raw: _fetched(raw, "# esp32-camera\n\n\nA   driver.\n", "text/plain; charset=utf-8")})
    page = web.read(raw)
    assert page.text == "# esp32-camera\nA driver." and page.title == "README.md"


def test_a_redirect_that_lands_on_list_keeps_the_final_url_and_host():
    landed = "https://raw.githubusercontent.com/espressif/esp32-camera/master/README.md"
    web, _, _, _ = _web({GITHUB_URL: Fetched(url=landed, content_type="text/plain", body=b"readme")})
    page = web.read(GITHUB_URL)
    assert page.url == landed and page.host == "raw.githubusercontent.com"


def test_a_page_that_somehow_lands_off_list_is_refused_after_the_fetch():
    # belt to the redirect handler's brace: a fetch that reports an off-list final URL is not read
    web, _, _, _ = _web({GITHUB_URL: Fetched(url="https://evil.example/x", content_type="text/html",
                                              body=b"<p>x</p>")})
    with pytest.raises(ResearchRefused) as exc:
        web.read(GITHUB_URL)
    assert "evil.example" in str(exc.value)


def test_reads_are_paced_and_cached_with_a_fake_clock():
    web, calls, slept, clock = _web({SEEED_URL: _fetched(SEEED_URL, DOC_HTML),
                                     GITHUB_URL: _fetched(GITHUB_URL, "<p>gh</p>")})
    web.read(SEEED_URL)
    web.read(SEEED_URL)                       # cached: no second fetch, no sleep
    assert calls == [SEEED_URL] and slept == []
    clock.t += 0.3                            # 0.3 s after the last fetch → the pacer waits the rest
    web.read(GITHUB_URL)
    assert calls == [SEEED_URL, GITHUB_URL] and len(slept) == 1 and 1.1 < slept[0] < 1.3
    clock.t += 601                            # the ten-minute cache has lapsed → a real re-read
    web.read(SEEED_URL)
    assert calls == [SEEED_URL, GITHUB_URL, SEEED_URL]


def test_the_search_and_reads_share_one_pacer():
    surl = "https://html.duckduckgo.com/html/?q=x"
    web, calls, slept, clock = _web({surl: _fetched(surl, DDG_HTML), SEEED_URL: _fetched(SEEED_URL, DOC_HTML)})
    web.search("x")
    web.read(SEEED_URL)                       # right after the search → paced
    assert calls == [surl, SEEED_URL] and len(slept) == 1 and slept[0] >= 1.4


def test_unavailable_is_raised_not_swallowed():
    web, _, _, _ = _web({})
    with pytest.raises(ResearchUnavailable) as exc:
        web.read(GITHUB_URL)
    assert "github.com answered HTTP 404" in str(exc.value)
    with pytest.raises(ResearchUnavailable):
        web.search("anything")


# ----- PDF -----

def test_a_pdf_is_read_through_the_injected_text_path_and_the_temp_file_is_removed():
    seen: list[Path] = []

    def pdf_text(path: Path) -> str:
        seen.append(Path(path))
        assert Path(path).is_file() and Path(path).suffix == ".pdf"
        assert Path(path).read_bytes().startswith(b"%PDF-1.4")
        return "ESP32-S3 Datasheet\n\nOperating voltage   3.0 V to 3.6 V\n"

    url = "https://www.espressif.com/sites/default/files/documentation/esp32-s3_datasheet_en.pdf"
    web, _, _, _ = _web({url: _fetched(url, b"%PDF-1.4 fake datasheet bytes", "application/pdf")},
                        pdf_text=pdf_text)
    page = web.read(url)
    assert page.text == "ESP32-S3 Datasheet\nOperating voltage 3.0 V to 3.6 V"
    assert page.title == "esp32-s3_datasheet_en.pdf" and page.host == "www.espressif.com"
    assert seen and not seen[0].exists()


def test_a_pdf_by_signature_counts_even_with_a_wrong_content_type():
    url = "https://www.ti.com/lit/ds/symlink/tmp117.pdf"
    web, _, _, _ = _web({url: _fetched(url, b"%PDF-1.7 bytes", "application/octet-stream")},
                        pdf_text=lambda p: "TMP117 text")
    assert web.read(url).text == "TMP117 text"


def test_pdf_refusals_are_plain():
    url = "https://www.ti.com/lit/ds/symlink/tmp117.pdf"
    no_reader, _, _, _ = _web({url: _fetched(url, b"%PDF-1.7", "application/pdf")})
    with pytest.raises(ResearchRefused) as exc:
        no_reader.read(url)
    assert "PDF" in str(exc.value) and "www.ti.com" in str(exc.value)
    empty, _, _, _ = _web({url: _fetched(url, b"%PDF-1.7", "application/pdf")}, pdf_text=lambda p: "")
    with pytest.raises(ResearchRefused) as exc:
        empty.read(url)
    assert "no readable text" in str(exc.value)
    huge, _, _, _ = _web({url: _fetched(url, b"%PDF-1.7", "application/pdf", complete=False)},
                         pdf_text=lambda p: "x")
    with pytest.raises(ResearchRefused) as exc:
        huge.read(url)
    assert "larger than 4 MB" in str(exc.value)


# ----- the service: model-facing text -----

def _service(pages: dict[str, Fetched] | None = None, **kw):
    web, calls, slept, clock = _web(pages or {}, **kw)
    mono = _Mono(1000.0)
    return ResearchService(web, mono=mono), calls, mono


def test_search_text_lists_hits_one_per_line_with_readability_and_the_exact_url():
    svc, _, _ = _service({SEARCH_URL: _fetched(SEARCH_URL, DDG_HTML)})
    text = svc.search_text("xiao esp32s3 sense camera")
    lines = text.splitlines()
    assert lines[0] == "Results for 'xiao esp32s3 sense camera' (DuckDuckGo, top 5):"
    assert lines[1] == ("1. Camera Usage in Seeed Studio XIAO ESP32S3 Sense — wiki.seeedstudio.com "
                        "(readable) — The XIAO ESP32S3 Sense integrates an OV2640 camera sensor and "
                        f"8MB PSRAM. — {SEEED_URL}")
    assert "www.amazon.com (not readable)" in lines[2] and AMAZON_URL in lines[2]
    assert "random-maker-blog.example (not readable)" in lines[3]
    assert "research_read" in lines[-1] and "not a verified fact" in lines[-1]
    assert svc.take_trail() == ["searched: xiao esp32s3 sense camera (5 hits)"]


def test_search_text_says_plainly_when_empty_or_unavailable():
    surl = "https://html.duckduckgo.com/html/?q=zzz"
    svc, _, _ = _service({surl: _fetched(surl, "<html><body>No results.</body></html>")})
    assert svc.search_text("zzz").startswith("No results came back for 'zzz'")
    assert svc.search_text("  ") == "What should I search for?"
    down, _, _ = _service({})
    out = down.search_text("esp32")
    assert out.startswith("The search didn't answer just now (") and "try again" in out
    assert down.take_trail()[0].startswith("search failed: esp32")


def test_read_text_leads_with_the_source_line_and_fences_the_page_as_data():
    svc, _, _ = _service({SEEED_URL: _fetched(SEEED_URL, DOC_HTML)})
    text = svc.read_text(SEEED_URL)
    lines = text.splitlines()
    assert lines[0] == "Read wiki.seeedstudio.com on 2026-09-04 — Camera Usage | Seeed Studio Wiki"
    assert lines[1] == SEEED_URL
    assert lines[2].startswith("[Page text HELIX read from wiki.seeedstudio.com — untrusted external CONTENT")
    assert "<<<PAGE-" in text and "PAGE-" in text.splitlines()[-1] and text.endswith("<<<")
    assert "8MB PSRAM" in text and "SIDEBAR-NAV-LINK" not in text
    assert svc.was_read(SEEED_URL) and not svc.was_read(GITHUB_URL)
    assert svc.take_trail() == [f"read: {SEEED_URL} ({len(extract_text(DOC_HTML)[1])} chars)"]


def test_read_text_with_a_question_returns_the_keyword_windows():
    intro = "".join(f"<p>Introductory paragraph {i} about unrelated shipping and packaging.</p>"
                    for i in range(60))
    outro = "".join(f"<p>Closing paragraph {i} about warranty and returns.</p>" for i in range(60))
    html = ("<html><body><main><h1>XIAO ESP32S3 Sense</h1>" + intro +
            "<h2>Memory</h2><p>The board carries 8MB PSRAM and 8MB flash on the module.</p>" + outro +
            "</main></body></html>")
    svc, _, _ = _service({SEEED_URL: _fetched(SEEED_URL, html)})
    text = svc.read_text(SEEED_URL, question="how much PSRAM does it have")
    assert "Passages mentioning psram" in text and "8MB PSRAM and 8MB flash" in text
    assert "Introductory paragraph 3 " not in text          # the far-away filler is not in a window
    assert "Closing paragraph 30 " not in text
    assert "Introductory paragraph 59 " in text             # …but the neighbouring lines are
    assert text.startswith("Read wiki.seeedstudio.com on 2026-09-04")
    miss = svc.read_text(SEEED_URL, question="bluetooth range")
    assert "No passage mentions 'bluetooth range' directly" in miss and "Introductory paragraph 0" in miss


def test_find_passages_merges_overlapping_windows_and_snaps_to_lines():
    text = "\n".join(["line zero is filler"] * 30 + ["The PSRAM is 8MB and the flash is 8MB.",
                                                     "Right after: PSRAM again."] + ["tail filler"] * 30)
    out, words = find_passages(text, "psram flash", window=20)
    assert words == ["flash", "psram"]
    assert out.count("PSRAM is 8MB") == 1                      # two hits, one merged window
    assert out.startswith("… ") and out.endswith(" …")
    assert "The PSRAM is 8MB and the flash is 8MB." in out    # snapped to the whole line
    assert find_passages(text, "the of and") == ("", [])       # stopwords only → nothing to look for
    assert find_passages("", "psram") == ("", [])


def test_read_text_refusals_name_the_host_and_say_why():
    svc, calls, _ = _service({})
    off = svc.read_text("https://random-maker-blog.example/esp32")
    assert off.startswith("I don't read random-maker-blog.example") and "documentation" in off
    plain = svc.read_text("http://docs.espressif.com/x")
    assert "https pages only" in plain and "docs.espressif.com" in plain
    amazon = svc.read_text(AMAZON_URL)
    assert "lookup_amazon" in amazon
    assert svc.read_text("") == "Which page? Give me the full https address."
    assert calls == []
    assert any(line.startswith("refused: https://random-maker-blog.example") for line in svc.take_trail())


def test_read_text_says_when_the_host_did_not_answer():
    svc, _, _ = _service({})
    out = svc.read_text(GITHUB_URL)
    assert out == "I couldn't read github.com just now (github.com answered HTTP 404) — try again in a moment."


def test_was_read_expires_and_take_trail_clears():
    svc, _, mono = _service({SEEED_URL: _fetched(SEEED_URL, DOC_HTML)})
    svc.read_text(SEEED_URL)
    assert svc.was_read(SEEED_URL) and svc.was_read(SEEED_URL.rstrip("/"))
    mono.t += 1801
    assert not svc.was_read(SEEED_URL)
    assert svc.was_read(SEEED_URL, within_s=10_000)
    assert svc.take_trail() and svc.take_trail() == [] and svc.trail() == []


def test_the_service_builds_its_own_adapter_with_the_apps_settings_and_clock():
    from helix.services import doc_extract

    class _AppClock:
        def now(self):
            return datetime(2026, 9, 4, 1, 0)

    svc = ResearchService(settings=_Settings(research_hosts_extra="docs.example.com"), clock=_AppClock())
    assert svc.readable("https://docs.example.com/x") and not svc.readable("https://other.example/x")
    assert svc.web._pdf_text is doc_extract.extract
    assert svc.web._wall() == datetime(2026, 9, 4, 1, 0)


# ----- the tools -----

class _Clock:
    def __init__(self, at: datetime = datetime(2026, 9, 4, 23, 30)) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _registry(tmp_path, pages=None, *, research=True, verified=True):
    svc, _calls, mono = _service(pages or {})
    store = VerifiedStore(JsonSettings(tmp_path / "helix_verified.json"), _Clock())
    reg = ToolRegistry(None, None)
    reg.attach_research(svc if research else None, store if verified else None)
    return reg, svc, store


def test_the_research_tools_appear_only_once_attached():
    bare = ToolRegistry(None, None)
    assert not set(RESEARCH_TOOLS) & {t.name for t in bare.specs()}
    assert bare.dispatch("research_search", {"query": "x"}).startswith("Unknown tool")


def test_the_research_tools_are_offered_and_route_to_the_service(tmp_path):
    surl = "https://html.duckduckgo.com/html/?q=esp32-camera"
    reg, _svc, _store = _registry(tmp_path, {surl: _fetched(surl, DDG_HTML),
                                             SEEED_URL: _fetched(SEEED_URL, DOC_HTML)})
    assert set(RESEARCH_TOOLS) <= {t.name for t in reg.specs()}
    assert reg.dispatch("research_search", {"query": "esp32-camera"}).startswith("Results for 'esp32-camera'")
    out = reg.dispatch("research_read", {"url": SEEED_URL, "question": "PSRAM"})
    assert out.startswith("Read wiki.seeedstudio.com on 2026-09-04") and "8MB PSRAM" in out


def test_only_forget_verified_is_fenced_and_note_is_dream_tier():
    assert "forget_verified" in BUILD_TOOLS
    for readable in ("research_search", "research_read", "verified_facts"):
        assert readable not in BUILD_TOOLS and readable not in DREAM_WRITES
    assert "note_verified_fact" in DREAM_WRITES and "note_verified_fact" not in BUILD_TOOLS


def test_note_verified_fact_refuses_a_source_helix_did_not_read(tmp_path):
    reg, _svc, store = _registry(tmp_path)
    out = reg.dispatch("note_verified_fact", {
        "claim": "XIAO ESP32S3 Sense PSRAM", "value": "8 MB", "source_url": SEEED_URL,
        "topics": ["esp32"]})
    assert out.startswith("I haven't read wiki.seeedstudio.com at that address this session")
    assert "research_read" in out and store.count() == 0


def test_note_verified_fact_refuses_off_list_and_amazon_sources_plainly(tmp_path):
    reg, _svc, store = _registry(tmp_path)
    off = reg.dispatch("note_verified_fact", {"claim": "c", "value": "v",
                                              "source_url": "https://random-maker-blog.example/p"})
    assert off.startswith("I don't read random-maker-blog.example")
    amz = reg.dispatch("note_verified_fact", {"claim": "c", "value": "v", "source_url": AMAZON_URL})
    assert "lookup_amazon" in amz
    assert store.count() == 0
    assert "needs both a claim" in reg.dispatch("note_verified_fact", {"claim": "", "value": "v",
                                                                       "source_url": SEEED_URL})
    assert "address of the page" in reg.dispatch("note_verified_fact", {"claim": "c", "value": "v"})


def test_note_verified_fact_records_a_fact_read_this_session_and_echoes_it(tmp_path):
    reg, _svc, store = _registry(tmp_path, {SEEED_URL: _fetched(SEEED_URL, DOC_HTML)})
    reg.dispatch("research_read", {"url": SEEED_URL})
    out = reg.dispatch("note_verified_fact", {
        "claim": "XIAO ESP32S3 Sense PSRAM", "value": "8MB", "source_url": SEEED_URL,
        "topics": ["esp32", "xiao"], "project": "hat cam", "confidence": "0.95"})
    fact = store.recent(1)[0]
    assert out == (f"Noted: XIAO ESP32S3 Sense PSRAM: 8MB — verified 2026-09-04 from "
                   f"wiki.seeedstudio.com [id {fact.id}] — HELIX carries it as verified knowledge "
                   "from now on.")
    assert fact.topics == ("esp32", "xiao") and fact.project == "hat cam" and fact.confidence == 0.95
    assert fact.source_url == SEEED_URL and fact.host == "wiki.seeedstudio.com"
    # noted again on a later day: refreshed, the first date kept, said so
    store._clock.at = datetime(2026, 9, 6, 2, 0)
    again = reg.dispatch("note_verified_fact", {"claim": "xiao esp32s3 sense psram", "value": "8 MB",
                                                "source_url": SEEED_URL})
    assert again.startswith("Noted (first verified 2026-09-04; refreshed): xiao esp32s3 sense psram: 8 MB")
    assert store.count() == 1 and store.recent(1)[0].value == "8 MB"


def test_note_verified_fact_without_a_research_service_refuses(tmp_path):
    reg, _svc, store = _registry(tmp_path, research=False)
    out = reg.dispatch("note_verified_fact", {"claim": "c", "value": "v", "source_url": SEEED_URL})
    assert "page reading isn't wired" in out and store.count() == 0


def test_verified_facts_reads_out_with_dates_hosts_and_ids(tmp_path):
    reg, _svc, store = _registry(tmp_path)
    psram = store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED_URL, topics=("esp32",), project="hat cam")
    store.note("BME280 supply voltage", "1.71 V to 3.6 V",
               "https://www.bosch-sensortec.com/products/environmental-sensors/humidity-sensors-bme280/",
               confidence=0.8, note="datasheet table 1")
    out = reg.dispatch("verified_facts", {"query": "esp32 psram"})
    assert out.splitlines()[0] == "Verified facts about 'esp32 psram' (1):"
    assert ("- XIAO ESP32S3 Sense PSRAM: 8 MB — verified 2026-09-04 from wiki.seeedstudio.com; "
            f"project hat cam [id {psram.id}] {SEEED_URL}") in out
    bme = reg.dispatch("verified_facts", {"query": "bme280 voltage"})
    assert "confidence 80%" in bme and "datasheet table 1" in bme and "www.bosch-sensortec.com" in bme
    assert reg.dispatch("verified_facts", {"query": "lidar"}).startswith("Nothing verified about 'lidar'")
    scoped = reg.dispatch("verified_facts", {"query": "psram", "project": "hat cam"})
    assert "for the project 'hat cam'" in scoped and "project hat cam" not in scoped


def test_forget_verified_by_id(tmp_path):
    reg, _svc, store = _registry(tmp_path)
    fact = store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED_URL)
    assert reg.dispatch("forget_verified", {"id": "nope"}) == "No verified fact has the id nope."
    assert reg.dispatch("forget_verified", {}) == "Which fact? Give me its id (verified_facts shows them)."
    assert reg.dispatch("forget_verified", {"id": fact.id}) == \
        "Dropped the verified fact: XIAO ESP32S3 Sense PSRAM: 8 MB."
    assert store.count() == 0


def test_readable_research_text_never_names_a_fenced_or_dream_write_tool(tmp_path):
    surl = "https://html.duckduckgo.com/html/?q=esp32"
    reg, _svc, store = _registry(tmp_path, {surl: _fetched(surl, DDG_HTML),
                                            SEEED_URL: _fetched(SEEED_URL, DOC_HTML)})
    store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED_URL)
    texts = [
        reg.dispatch("research_search", {"query": "esp32"}),
        reg.dispatch("research_read", {"url": SEEED_URL}),
        reg.dispatch("research_read", {"url": "https://random-maker-blog.example/x"}),
        reg.dispatch("research_read", {"url": AMAZON_URL}),
        reg.dispatch("verified_facts", {"query": "esp32"}),
        reg.dispatch("verified_facts", {"query": "nothing here"}),
    ]
    for text in texts:
        for fenced in sorted(BUILD_TOOLS | DREAM_WRITES):
            assert fenced not in text, (fenced, text[:80])


def test_every_research_tool_has_a_spoken_phrase():
    for tool in RESEARCH_TOOLS:
        label = friendly_tool_label(tool)
        assert label != "Working…" and "_" not in label, tool
