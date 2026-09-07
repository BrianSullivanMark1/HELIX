"""ChromeCart — the cart that actually pops up. Edge/I-O.

WHY THIS EXISTS. Amazon's remote add-to-cart link (the old one-URL handoff) now bounces every open
through a sign-in page that demands a fresh password whenever the session's last authentication is
more than an hour old — verified live: every variant of the link 302s to /ap/signin with
openid.pape.max_auth_age=3600. So "open the cart" landed the user on a password prompt, not a cart,
and the staged quantities never showed up anywhere they could see.

WHAT THIS DOES INSTEAD. HELIX drives a Chrome window of its OWN (a dedicated profile folder, never
the user's everyday browser profile) through the DevTools protocol over localhost: for each staged
item it opens the real product page, sets the quantity picker, presses Amazon's own Add-to-Cart
button, and finally opens Amazon's cart page and READS IT BACK — so the report is what the cart
holds, not what was hoped. Amazon keeps a guest cart per browser profile, so this works before the
user has ever signed in; the first time they check out (or sign in) in this window, Amazon merges
that cart into their account, and every later run lands straight in the account cart. Signing the
window into an Amazon Business account makes every HELIX-built cart a business order.

WHAT IT NEVER DOES. It presses one button on a product page and one URL for the cart view. It never
touches checkout, "Buy now", 1-Click, payment, or address pages, and it never types into a form — the
user does the buying, on Amazon, with their own hands.

Reuse without a fight: Chrome writes its DevTools port to DevToolsActivePort inside the profile; a
window left open from an earlier run is found through that file and driven again rather than
relaunched (a second launch on a busy profile just focuses the first and exits without a port).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from helix.domain.shopping import CartItem
from helix.logging_setup import get_logger

_LOG = get_logger("chromecart")

_LAUNCH_WAIT_S = 15.0
_NAV_TIMEOUT_S = 30.0
_CONFIRM_WAIT_S = 7.0
_MAX_ROUNDS = 3  # add rounds per item when the quantity picker caps below the wanted count

CART_URL = "https://www.amazon.com/gp/cart/view.html"
AUTO = object()  # chrome_path default: discover the browser on first use


class ChromeCartError(Exception):
    """The driver couldn't do its job (no Chrome, no DevTools port, a dead socket)."""


@dataclass(frozen=True)
class AddResult:
    asin: str
    label: str
    wanted: int
    added: int          # quantity this run pressed Add-to-Cart for (0 = nothing added)
    ok: bool
    reason: str = ""    # why not (needs-option, buying-options, no-button, robot, timeout, …)
    title: str = ""     # the product page's title (what was actually added)


@dataclass(frozen=True)
class CartRow:
    asin: str
    quantity: int
    price: float | None = None
    title: str = ""


@dataclass(frozen=True)
class CartState:
    rows: tuple[CartRow, ...]
    subtotal: str = ""     # "$40.97" as Amazon prints it
    account: str = ""      # the nav greeting ("Hello, Brian" / "Hello, sign in")
    url: str = ""

    @property
    def signed_in(self) -> bool | None:
        a = self.account.lower()
        if not a:
            return None
        return "sign in" not in a

    def quantity_of(self, asin: str) -> int:
        return sum(r.quantity for r in self.rows if r.asin == asin)


# ----- in-page scripts (run by Runtime.evaluate; results come back as JSON values) -----
_ADD_JS = """
(() => {
  const out = {ok: false, reason: "", title: "", qty: 0, max: null};
  const t = document.getElementById("productTitle");
  out.title = t ? t.textContent.trim().slice(0, 160) : document.title.slice(0, 160);
  if (/robot check|captcha/i.test(document.title)) { out.reason = "robot"; return out; }
  const btn = document.getElementById("add-to-cart-button")
           || document.querySelector('input[name="submit.add-to-cart"]');
  if (!btn) {
    if (document.getElementById("buybox-see-all-buying-choices")) out.reason = "buying-options";
    else if (document.querySelector('#twister, [id^="variation_"]')) out.reason = "needs-option";
    else if (/currently unavailable/i.test((document.getElementById("availability") || {}).textContent || "")) out.reason = "unavailable";
    else out.reason = "no-button";
    return out;
  }
  const want = %WANT%;
  let q = 1;
  const sel = document.getElementById("quantity");
  if (sel && sel.tagName === "SELECT") {
    const nums = Array.from(sel.options).map(o => parseInt(o.value, 10)).filter(n => !isNaN(n));
    if (nums.length) {
      out.max = Math.max(...nums);
      q = Math.min(want, out.max);
      sel.value = String(q);
      sel.dispatchEvent(new Event("change", {bubbles: true}));
    }
  }
  out.qty = q;
  btn.click();
  out.ok = true;
  return out;
})()
"""

_CONFIRM_JS = """
(() => {
  const href = location.href;
  const hit = document.querySelector('#attach-added-to-cart-message, #huc-v2-order-row-confirm-text,'
    + ' #NATC_SMART_WAGON_CONF_MSG_SUCCESS, #sw-atc-details-single-container, #attach-accessory-cart-status,'
    + ' [data-csa-c-content-id="sw-atc-confirmation"], .sw-atc-message, #sw-gtc-confirmation-msg');
  return {href: href, confirmed: Boolean(hit) || /\\/cart\\/|smart-wagon|huc/i.test(href)};
})()
"""

_READ_JS = """
(() => {
  const seen = new Set();
  const rows = [];
  const scope = document.querySelector('#sc-active-cart') || document;
  scope.querySelectorAll('[data-asin][data-quantity]').forEach(e => {
    const asin = (e.getAttribute('data-asin') || '').toUpperCase();
    if (asin.length !== 10 || seen.has(asin)) return;
    seen.add(asin);
    const qty = parseInt(e.getAttribute('data-quantity'), 10) || 0;
    const p = parseFloat((e.getAttribute('data-price') || '').replace(/[^0-9.]/g, ''));
    const tn = e.querySelector('.sc-product-title, .a-truncate-cut, [data-a-truncate], .sc-product-link');
    rows.push({asin: asin, qty: qty, price: isNaN(p) ? null : p,
               title: tn ? tn.textContent.trim().replace(/\\s+/g, ' ').slice(0, 120) : ''});
  });
  const sub = document.querySelector('#sc-subtotal-amount-activecart, #sc-subtotal-amount-buybox');
  const acct = document.querySelector('#nav-link-accountList-nav-line-1');
  return {rows: rows, subtotal: sub ? sub.textContent.trim() : '',
          account: acct ? acct.textContent.trim() : '', url: location.href};
})()
"""


# ----- finding a browser -----
def find_chrome() -> str | None:
    """chrome.exe (or msedge.exe as a fallback — same DevTools protocol) on this machine."""
    override = (os.environ.get("HELIX_CHROME") or "").strip()
    if override and Path(override).is_file():
        return override
    if sys.platform == "win32":
        try:
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for exe in ("chrome.exe", "msedge.exe"):
                    try:
                        with winreg.OpenKey(
                                hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                            path, _ = winreg.QueryValueEx(k, None)
                            if path and Path(path).is_file():
                                return str(path)
                    except OSError:
                        continue
        except ImportError:  # pragma: no cover - non-Windows
            pass
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        for root in roots:
            for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
                p = Path(root) / rel
                if root and p.is_file():
                    return str(p)
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser", "msedge"):
        hit = shutil.which(name)
        if hit:
            return hit
    return None


# ----- a minimal DevTools session -----
class _Session:
    """One page target over its DevTools WebSocket. Commands are synchronous; events buffer."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next = 0
        self.events: deque = deque(maxlen=400)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def send(self, method: str, params: dict | None = None, *, timeout: float = 20.0) -> dict:
        self._next += 1
        mid = self._next
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ChromeCartError(f"DevTools {method} timed out")
            try:
                raw = self._ws.recv(timeout=left)
            except TimeoutError as exc:
                raise ChromeCartError(f"DevTools {method} timed out") from exc
            except Exception as exc:  # noqa: BLE001 - a closed socket
                raise ChromeCartError(f"DevTools socket died during {method}") from exc
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise ChromeCartError(f"DevTools {method}: {msg['error'].get('message', 'error')}")
                return msg.get("result") or {}
            if "method" in msg:
                self.events.append(msg)

    def wait_event(self, name: str, *, timeout: float) -> bool:
        """True once `name` has fired (buffered or arriving within `timeout`)."""
        for ev in list(self.events):
            if ev.get("method") == name:
                self.events.remove(ev)
                return True
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                raw = self._ws.recv(timeout=left)
            except TimeoutError:
                return False
            except Exception:  # noqa: BLE001
                return False
            msg = json.loads(raw)
            if msg.get("method") == name:
                return True
            if "method" in msg:
                self.events.append(msg)

    def evaluate(self, expression: str, *, timeout: float = 15.0):
        res = self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout)
        if "exceptionDetails" in res:
            raise ChromeCartError("page script failed: "
                                  + str(res["exceptionDetails"].get("text", ""))[:120])
        return (res.get("result") or {}).get("value")

    def navigate(self, url: str, *, timeout: float = _NAV_TIMEOUT_S) -> None:
        self.send("Page.enable")
        self.events.clear()
        self.send("Page.navigate", {"url": url})
        if not self.wait_event("Page.loadEventFired", timeout=timeout):
            raise ChromeCartError(f"the page didn't finish loading: {url}")


def _http_json(url: str, *, method: str = "GET", timeout: float = 3.0):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost DevTools only
        return json.loads(resp.read().decode("utf-8", "replace") or "null")


class ChromeCart:
    """Drive HELIX's own Chrome window to build the user's Amazon cart, then read it back.

    Injectables (tests): `chrome_path` (default: auto-find; None = no browser), `launcher(cmd) -> Popen-like`,
    `http(url, method) -> json`, `ws_connect(url) -> websocket`, `clock`, `sleep`.
    """

    def __init__(self, profile_dir: Path, *, chrome_path=AUTO, launcher=None,
                 http=None, ws_connect=None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._profile = Path(profile_dir)
        self._chrome: str | None = None if chrome_path is AUTO else chrome_path
        self._chrome_resolved = chrome_path is not AUTO
        self._launch = launcher or self._spawn
        self._http = http or _http_json
        self._ws_connect = ws_connect or self._connect_ws
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._proc = None

    # ----- discovery -----
    def chrome_path(self) -> str | None:
        if not self._chrome_resolved:
            self._chrome = find_chrome()
            self._chrome_resolved = True
        return self._chrome

    def available(self) -> bool:
        return self.chrome_path() is not None

    # ----- lifecycle -----
    @staticmethod
    def _spawn(cmd: list[str]):
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        return subprocess.Popen(cmd, close_fds=True, creationflags=flags,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    @staticmethod
    def _connect_ws(url: str):
        from websockets.sync.client import connect  # imported lazily: not needed until a cart opens

        return connect(url, max_size=None, open_timeout=5, close_timeout=2)

    def _port_file(self) -> Path:
        return self._profile / "DevToolsActivePort"

    def _read_port(self) -> int | None:
        try:
            first = self._port_file().read_text(encoding="utf-8").splitlines()[0].strip()
            return int(first)
        except (OSError, ValueError, IndexError):
            return None

    def _alive(self, port: int | None) -> bool:
        if not port:
            return False
        try:
            info = self._http(f"http://127.0.0.1:{port}/json/version")
        except Exception:  # noqa: BLE001
            return False
        return isinstance(info, dict) and "webSocketDebuggerUrl" in info

    def _endpoint(self) -> int:
        """The DevTools port of a live HELIX cart window — reused if one is open, launched if not."""
        port = self._read_port()
        if self._alive(port):
            return port  # type: ignore[return-value]
        chrome = self.chrome_path()
        if chrome is None:
            raise ChromeCartError("no Chrome (or Edge) found on this machine")
        self._profile.mkdir(parents=True, exist_ok=True)
        try:
            self._port_file().unlink()
        except OSError:
            pass
        cmd = [
            chrome, "--remote-debugging-port=0", f"--user-data-dir={self._profile}",
            "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--disable-features=Translate,PrivacySandboxSettings4", "--window-size=1240,920",
            "--new-window", "about:blank",
        ]
        self._proc = self._launch(cmd)
        deadline = self._clock() + _LAUNCH_WAIT_S
        while self._clock() < deadline:
            port = self._read_port()
            if self._alive(port):
                return port  # type: ignore[return-value]
            self._sleep(0.25)
        raise ChromeCartError("Chrome started but never opened its DevTools port (is another window "
                              "already using HELIX's cart profile?)")

    def _open_tab(self, port: int, url: str = "about:blank") -> tuple[str, _Session]:
        info = self._http(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
        if not isinstance(info, dict) or "webSocketDebuggerUrl" not in info:
            raise ChromeCartError("Chrome wouldn't open a tab")
        return str(info.get("id") or ""), _Session(self._ws_connect(info["webSocketDebuggerUrl"]))

    def _close_tab(self, port: int, tab_id: str) -> None:
        if not tab_id:
            return
        try:
            self._http(f"http://127.0.0.1:{port}/json/close/{tab_id}")
        except Exception:  # noqa: BLE001
            pass

    # ----- the work -----
    def add_items(self, items: list[CartItem], *, on_progress: Callable[[str], None] | None = None,
                  ) -> tuple[list[AddResult], CartState | None]:
        """Press Add-to-Cart for every item at its quantity, then open and read the cart. Returns
        the per-item results and the cart as read (None when the read itself failed)."""
        with self._lock:
            port = self._endpoint()
            results: list[AddResult] = []
            tab_id, s = self._open_tab(port)
            try:
                for item in items:
                    if on_progress:
                        on_progress(f"Adding {item.label} ×{item.quantity} to the Amazon cart…")
                    results.append(self._add_one(s, item))
                s.navigate(CART_URL)
                self._sleep(0.6)
                state = self._read_state(s)
                try:
                    s.send("Page.bringToFront", timeout=5)
                except ChromeCartError:
                    pass
            finally:
                s.close()
            # The work tab stays open ON the cart page — that is the window the user reviews.
            # Housekeeping: close any other blank tabs this driver left behind earlier.
            self._close_blank_tabs(port, keep=tab_id)
            return results, state

    def _add_one(self, s: _Session, item: CartItem) -> AddResult:
        remaining = item.quantity
        added = 0
        title = ""
        reason = ""
        for _ in range(_MAX_ROUNDS):
            try:
                s.navigate(f"https://www.amazon.com/dp/{item.asin}?th=1&psc=1")
                self._sleep(0.5)
                res = s.evaluate(_ADD_JS.replace("%WANT%", str(remaining)))
            except ChromeCartError as exc:
                reason = f"timeout ({exc})"
                break
            if not isinstance(res, dict):
                reason = "no-result"
                break
            title = str(res.get("title") or title)[:160]
            if not res.get("ok"):
                reason = str(res.get("reason") or "no-button")
                break
            got = int(res.get("qty") or 1)
            self._wait_confirm(s)
            added += got
            remaining -= got
            if remaining <= 0:
                break
        ok = added > 0 and not reason
        if added > 0 and remaining > 0 and not reason:
            reason = f"the picker capped at {added}; {remaining} more not added"
        return AddResult(asin=item.asin, label=item.label, wanted=item.quantity, added=added,
                         ok=ok or added > 0, reason=reason, title=title)

    def _wait_confirm(self, s: _Session) -> bool:
        deadline = self._clock() + _CONFIRM_WAIT_S
        while self._clock() < deadline:
            self._sleep(0.5)
            try:
                res = s.evaluate(_CONFIRM_JS, timeout=5)
            except ChromeCartError:
                continue  # mid-navigation: the context is gone for a moment
            if isinstance(res, dict) and res.get("confirmed"):
                return True
        return False

    def _read_state(self, s: _Session) -> CartState | None:
        try:
            res = s.evaluate(_READ_JS)
        except ChromeCartError:
            return None
        if not isinstance(res, dict):
            return None
        rows = []
        for r in res.get("rows") or []:
            try:
                rows.append(CartRow(asin=str(r.get("asin") or "").upper(),
                                    quantity=int(r.get("qty") or 0),
                                    price=float(r["price"]) if r.get("price") is not None else None,
                                    title=str(r.get("title") or "")))
            except (TypeError, ValueError):
                continue
        return CartState(rows=tuple(rows), subtotal=str(res.get("subtotal") or ""),
                         account=str(res.get("account") or ""), url=str(res.get("url") or ""))

    def read_cart(self, *, show: bool = False) -> CartState | None:
        """Open the cart page in the HELIX window and read what it holds. `show` raises the window."""
        with self._lock:
            port = self._endpoint()
            tab_id, s = self._open_tab(port)
            try:
                s.navigate(CART_URL)
                self._sleep(0.6)
                state = self._read_state(s)
                if show:
                    try:
                        s.send("Page.bringToFront", timeout=5)
                    except ChromeCartError:
                        pass
            finally:
                s.close()
            if not show:
                self._close_tab(port, tab_id)
            else:
                self._close_blank_tabs(port, keep=tab_id)
            return state

    def _close_blank_tabs(self, port: int, *, keep: str) -> None:
        try:
            tabs = self._http(f"http://127.0.0.1:{port}/json/list")
        except Exception:  # noqa: BLE001
            return
        for t in tabs or []:
            if not isinstance(t, dict) or t.get("id") == keep:
                continue
            if t.get("type") == "page" and (t.get("url") or "") in ("about:blank", "chrome://newtab/"):
                self._close_tab(port, str(t.get("id") or ""))
