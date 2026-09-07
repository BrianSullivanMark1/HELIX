"""The Chrome cart driver, exercised against a fake DevTools endpoint: launch-and-wait, reuse of a
window left open, the per-item add flow (quantity picker, capped pickers, listings that need a
click), the cart read-back, and the failure shapes. The live Amazon mechanics were verified by hand
(a guest cart, two items at 2 and 1, read back at $25.67); these pin the protocol plumbing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helix.adapters.chrome_cart import (
    CART_URL,
    CartRow,
    CartState,
    ChromeCart,
    ChromeCartError,
    find_chrome,
)
from helix.domain.shopping import CartItem


class _Ws:
    """A scripted page target: answers each command, fires a load event after every navigate,
    and hands back the next scripted value for every Runtime.evaluate."""

    def __init__(self, evals: list):
        self.evals = list(evals)
        self.sent: list[dict] = []
        self.queue: list[str] = []
        self.closed = False

    def send(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        m = msg["method"]
        if m == "Runtime.evaluate":
            value = self.evals.pop(0) if self.evals else None
            if isinstance(value, Exception):
                self.queue.append(json.dumps({"id": msg["id"], "result": {
                    "exceptionDetails": {"text": str(value)}}}))
            else:
                self.queue.append(json.dumps({"id": msg["id"], "result": {"result": {"value": value}}}))
        elif m == "Page.navigate":
            self.queue.append(json.dumps({"id": msg["id"], "result": {"frameId": "f"}}))
            self.queue.append(json.dumps({"method": "Page.loadEventFired", "params": {}}))
        else:
            self.queue.append(json.dumps({"id": msg["id"], "result": {}}))

    def recv(self, timeout=None) -> str:
        if not self.queue:
            raise TimeoutError()
        return self.queue.pop(0)

    def close(self) -> None:
        self.closed = True


class _Rig:
    def __init__(self, tmp_path: Path, evals: list, *, alive_at_start: bool = False):
        self.profile = tmp_path / "prof"
        self.launched: list[list[str]] = []
        self.http_calls: list[str] = []
        self.ws = _Ws(evals)
        self.alive = alive_at_start
        self.port = 9333
        if alive_at_start:
            self.profile.mkdir(parents=True)
            (self.profile / "DevToolsActivePort").write_text(f"{self.port}\n/devtools/browser/x", "utf-8")

    def launcher(self, cmd):
        self.launched.append(cmd)
        # Chrome writes its port file shortly after launch.
        (self.profile / "DevToolsActivePort").write_text(f"{self.port}\n/devtools/browser/x", "utf-8")
        self.alive = True
        return object()

    def http(self, url, method="GET"):
        self.http_calls.append(f"{method} {url}")
        if not self.alive:
            raise OSError("connection refused")
        if url.endswith("/json/version"):
            return {"webSocketDebuggerUrl": "ws://127.0.0.1/browser"}
        if "/json/new" in url:
            return {"id": "tab1", "webSocketDebuggerUrl": "ws://127.0.0.1/page/tab1"}
        if url.endswith("/json/list"):
            return [{"id": "tab1", "type": "page", "url": CART_URL},
                    {"id": "old", "type": "page", "url": "about:blank"}]
        return {}

    def connect(self, url):
        return self.ws

    def driver(self, *, chrome="C:/fake/chrome.exe") -> ChromeCart:
        t = {"now": 0.0}
        return ChromeCart(self.profile, chrome_path=chrome, launcher=self.launcher, http=self.http,
                          ws_connect=self.connect, clock=lambda: t["now"],
                          sleep=lambda s: t.__setitem__("now", t["now"] + s))


def _navs(ws: _Ws) -> list[str]:
    return [m["params"]["url"] for m in ws.sent if m["method"] == "Page.navigate"]


def test_add_items_launches_chrome_presses_add_to_cart_and_reads_the_cart_back(tmp_path):
    rig = _Rig(tmp_path, evals=[
        {"ok": True, "qty": 2, "title": "8Pcs Speaker", "max": 30},   # add: speakers ×2
        {"href": "https://www.amazon.com/cart/smart-wagon", "confirmed": True},
        {"ok": True, "qty": 1, "title": "INMP441 5-pack", "max": 30},  # add: mic ×1
        {"href": "https://www.amazon.com/dp/x", "confirmed": True},
        {"rows": [{"asin": "B0C49RZ9WJ", "qty": 2, "price": 6.99, "title": "8Pcs Speaker"},
                  {"asin": "B0C1C64R8S", "qty": 1, "price": 11.69, "title": "INMP441"}],
         "subtotal": "$25.67", "account": "Hello, sign in", "url": CART_URL},
    ])
    d = rig.driver()
    items = [CartItem("speakers", "B0C49RZ9WJ", 2, 6.99), CartItem("mic", "B0C1C64R8S", 1)]
    progress: list[str] = []
    results, state = d.add_items(items, on_progress=progress.append)

    assert len(rig.launched) == 1
    cmd = rig.launched[0]
    assert cmd[0] == "C:/fake/chrome.exe" and "--remote-debugging-port=0" in cmd
    assert any(a == f"--user-data-dir={rig.profile}" for a in cmd)
    assert [r.added for r in results] == [2, 1] and all(r.ok for r in results)
    assert results[0].title == "8Pcs Speaker"
    assert _navs(rig.ws) == ["https://www.amazon.com/dp/B0C49RZ9WJ?th=1&psc=1",
                             "https://www.amazon.com/dp/B0C1C64R8S?th=1&psc=1", CART_URL]
    # The add script carried the wanted quantity into the page.
    add_scripts = [m["params"]["expression"] for m in rig.ws.sent if m["method"] == "Runtime.evaluate"]
    assert "const want = 2;" in add_scripts[0] and "const want = 1;" in add_scripts[2]
    assert state.subtotal == "$25.67" and state.signed_in is False
    assert state.quantity_of("B0C49RZ9WJ") == 2 and state.quantity_of("B0NOTTHERE") == 0
    assert progress == ["Adding speakers ×2 to the Amazon cart…", "Adding mic ×1 to the Amazon cart…"]
    assert any(m["method"] == "Page.bringToFront" for m in rig.ws.sent)
    assert rig.ws.closed
    assert "GET http://127.0.0.1:9333/json/close/old" in rig.http_calls  # stray blank tab tidied


def test_a_window_left_open_is_reused_not_relaunched(tmp_path):
    rig = _Rig(tmp_path, alive_at_start=True, evals=[
        {"rows": [], "subtotal": "", "account": "Hello, Brian", "url": CART_URL},
    ])
    d = rig.driver()
    state = d.read_cart()
    assert rig.launched == []
    assert state.rows == () and state.signed_in is True
    assert "GET http://127.0.0.1:9333/json/close/tab1" in rig.http_calls  # a silent read closes its tab


def test_a_capped_quantity_picker_adds_in_rounds(tmp_path):
    rig = _Rig(tmp_path, evals=[
        {"ok": True, "qty": 3, "title": "Screws", "max": 3},      # round 1: picker caps at 3
        {"href": "", "confirmed": True},
        {"ok": True, "qty": 2, "title": "Screws", "max": 3},      # round 2: the remaining 2
        {"href": "", "confirmed": True},
        {"rows": [{"asin": "B08N5WRWNW", "qty": 5, "price": 1.0, "title": "Screws"}],
         "subtotal": "$5.00", "account": "", "url": CART_URL},
    ])
    results, state = rig.driver().add_items([CartItem("screws", "B08N5WRWNW", 5)])
    assert results[0].added == 5 and results[0].ok and results[0].reason == ""
    assert _navs(rig.ws).count("https://www.amazon.com/dp/B08N5WRWNW?th=1&psc=1") == 2
    assert state.signed_in is None  # no nav greeting read → unknown, never asserted


def test_a_listing_that_needs_a_click_is_reported_not_faked(tmp_path):
    rig = _Rig(tmp_path, evals=[
        {"ok": False, "reason": "needs-option", "title": "Filament", "qty": 0},
        {"rows": [], "subtotal": "", "account": "", "url": CART_URL},
    ])
    results, _ = rig.driver().add_items([CartItem("filament", "B0DCJR8JTG", 1)])
    assert results[0].added == 0 and not results[0].ok and results[0].reason == "needs-option"
    assert results[0].title == "Filament"


def test_no_chrome_is_a_clean_error(tmp_path):
    rig = _Rig(tmp_path, evals=[])
    with pytest.raises(ChromeCartError):
        rig.driver(chrome=None).add_items([CartItem("x", "B08N5WRWNW", 1)])
    assert rig.launched == []


def test_a_chrome_that_never_opens_its_port_times_out(tmp_path):
    rig = _Rig(tmp_path, evals=[])

    def dead_launch(cmd):
        rig.launched.append(cmd)
        return object()  # no port file ever appears

    d = ChromeCart(rig.profile, chrome_path="C:/fake/chrome.exe", launcher=dead_launch, http=rig.http,
                   ws_connect=rig.connect, clock=iter(range(0, 10_000)).__next__, sleep=lambda s: None)
    with pytest.raises(ChromeCartError, match="never opened its DevTools port"):
        d.read_cart()


def test_page_script_failures_surface_as_driver_errors_and_a_read_swallows_them(tmp_path):
    from helix.adapters.chrome_cart import _Session

    with pytest.raises(ChromeCartError, match="page script failed"):
        _Session(_Ws([RuntimeError("boom")])).evaluate("1+1")
    class _Mute(_Ws):
        def send(self, text: str) -> None:  # a socket that never answers
            pass

    with pytest.raises(ChromeCartError, match="timed out"):
        _Session(_Mute([])).send("Page.enable", timeout=0.01)
    rig = _Rig(tmp_path, evals=[RuntimeError("boom")])
    assert rig.driver().read_cart() is None  # a broken cart read is "couldn't read", not a crash


def test_cart_state_helpers():
    st = CartState(rows=(CartRow("B08N5WRWNW", 2, 1.5), CartRow("B08N5WRWNW", 1)), account="Hello, sign in")
    assert st.quantity_of("b08n5wrwnw".upper()) == 3 and st.signed_in is False
    assert CartState(rows=(), account="Hello, Brian").signed_in is True


def test_find_chrome_returns_a_path_or_none():
    hit = find_chrome()
    assert hit is None or Path(hit).is_file()


def test_available_reflects_discovery(tmp_path):
    assert ChromeCart(tmp_path, chrome_path="C:/fake/chrome.exe").available()
    assert not ChromeCart(tmp_path, chrome_path=None).available()
