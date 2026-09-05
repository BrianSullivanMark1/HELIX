"""The Amazon faculty, second cut: HELIX's own eyes (search/lookup), verification before staging, the
staged list that survives restarts, the Chrome-driven handoff that reads the cart back, the
parts-list bridge, the face's cart events, and the fences."""
from __future__ import annotations

from helix.adapters.chrome_cart import AddResult, CartRow, CartState, ChromeCartError
from helix.adapters.signal_bus import SignalBus
from helix.domain.amazon import Listing, Product
from helix.domain.events import CartChanged, ProductsFound
from helix.services.conversation import BUILD_TOOLS
from helix.services.parts import PartsService
from helix.services.shopping import ShoppingService
from helix.services.tools import ToolRegistry


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


class _Web:
    """A fake AmazonWeb: a catalog of listings, a canned search, and a call log."""

    def __init__(self, listings=None, results=None, fail=False):
        self.listings = listings or {}
        self.results = results or []
        self.fail = fail
        self.calls: list[str] = []

    def search(self, query, *, limit=10):
        self.calls.append(f"search:{query}")
        if self.fail:
            raise RuntimeError("Amazon is showing its robot check")
        return self.results[:limit]

    def listing(self, asin):
        self.calls.append(f"listing:{asin}")
        if self.fail:
            raise RuntimeError("Amazon is showing its robot check")
        return self.listings.get(asin)


class _Driver:
    def __init__(self, results=None, state=None, raise_exc=None, available=True):
        self.results = results or []
        self.state = state
        self.raise_exc = raise_exc
        self._available = available
        self.calls: list = []

    def available(self):
        return self._available

    def add_items(self, items, *, on_progress=None):
        self.calls.append(("add", [(i.asin, i.quantity) for i in items]))
        if on_progress:
            on_progress("adding…")
        if self.raise_exc:
            raise self.raise_exc
        return self.results, self.state

    def read_cart(self, *, show=False):
        self.calls.append(("read", show))
        if self.raise_exc:
            raise self.raise_exc
        return self.state


SPEAKER = Listing(asin="B0C49RZ9WJ", title="8Pcs 8 Ohm 2W Speaker 28mm", price=6.99, image="https://m.media-amazon.com/x.jpg")
MIC = Listing(asin="B0C1C64R8S", title="5Pcs INMP441 Microphone Module", price=11.69, can_add=False)
RESULTS = [Product("B0C1C64R8S", "5Pcs INMP441 Microphone Module", price=11.69, rating=4.6, reviews=88, prime=True,
                   image="https://m.media-amazon.com/mic.jpg"),
           Product("B0FKFR1WFX", "3 PCS INMP441", price=8.99, rating=4.4, reviews=32),
           Product("B0SPONSORD", "Fancy mic", price=39.99, sponsored=True)]


def _svc(**kw):
    kw.setdefault("opener", lambda url: True)
    kw.setdefault("clock", lambda: "2026-09-04T18:00")
    return ShoppingService(**kw)


# ----- eyes -----
def test_search_lists_live_results_for_the_model_and_shows_cards_to_the_face():
    bus = SignalBus()
    seen: list[ProductsFound] = []
    bus.subscribe(ProductsFound, seen.append)
    s = _svc(web=_Web(results=RESULTS), bus=bus)
    text = s.search("  INMP441  I2S ")
    assert "Amazon results for 'INMP441 I2S' (live, top 3)" in text
    assert "1. 5Pcs INMP441 Microphone Module — $11.69; 4.6 stars (88 ratings); Prime — ASIN B0C1C64R8S" in text
    assert "3. Fancy mic — $39.99; no rating; sponsored — ASIN B0SPONSORD" in text
    assert len(seen) == 1 and seen[0].title == "Amazon: INMP441 I2S"
    assert seen[0].items[0]["image"] == "https://m.media-amazon.com/mic.jpg"
    assert seen[0].items[0]["url"] == "https://www.amazon.com/dp/B0C1C64R8S"


def test_search_honours_a_budget_and_says_what_it_dropped():
    s = _svc(web=_Web(results=RESULTS))
    text = s.search("mic", budget=10)
    assert "B0FKFR1WFX" in text and "B0C1C64R8S" not in text and "2 results over the $10.00 budget" in text


def test_search_failure_falls_back_honestly_and_never_pretends_nothing_matched():
    text = _svc(web=_Web(fail=True)).search("mic")
    assert "Amazon didn't answer HELIX's own search" in text and "robot check" in text
    assert "web search" in text
    assert "aren't wired" in _svc().search("mic")
    assert "What should I search" in _svc(web=_Web()).search("  ")


def test_lookup_reads_one_listing_and_refuses_dead_ids():
    s = _svc(web=_Web(listings={"B0C49RZ9WJ": SPEAKER}))
    text = s.lookup("https://www.amazon.com/dp/B0C49RZ9WJ?ref=x")
    assert text.startswith("8Pcs 8 Ohm 2W Speaker 28mm (ASIN B0C49RZ9WJ)") and "$6.99" in text
    assert "no product page for B0DEADDEAD" in s.lookup("B0DEADDEAD")
    assert "isn't an Amazon product id" in s.lookup("some words")


# ----- verification before staging -----
def test_add_verifies_the_listing_and_records_the_price_it_read():
    web = _Web(listings={"B0C49RZ9WJ": SPEAKER})
    s = _svc(web=web)
    out = s.add([{"name": "28mm speakers", "asin": "B0C49RZ9WJ", "quantity": 2, "price": 99}])
    assert "Staged: 28mm speakers x2 at $6.99 (live)" in out  # the READ price beats the model's 99
    item = s._items[0]
    assert item.title == "8Pcs 8 Ohm 2W Speaker 28mm" and item.image.endswith("x.jpg")
    assert web.calls == ["listing:B0C49RZ9WJ"]


def test_add_refuses_an_id_amazon_has_no_page_for_by_name():
    s = _svc(web=_Web(listings={}))
    out = s.add([{"name": "ghost part", "asin": "B0DEADDEAD"}])
    assert "Couldn't stage: ghost part — Amazon has no product page for B0DEADDEAD" in out
    assert "search_amazon" in out and s._items == []


def test_add_uses_the_search_catalog_instead_of_refetching():
    web = _Web(results=RESULTS, listings={})
    s = _svc(web=web)
    s.search("mic")
    out = s.add([{"name": "INMP441 5-pack", "asin": "B0C1C64R8S"}])
    assert "at $11.69 (live)" in out and web.calls == ["search:mic"]  # no listing fetch needed


def test_add_stages_unverified_when_amazon_wont_answer_and_says_so():
    s = _svc(web=_Web(fail=True))
    out = s.add([{"name": "screws", "asin": "B08N5WRWNW", "price": "3.50"}])
    assert "Staged: screws x1 at $3.50 (as given) [unverified: Amazon didn't answer" in out
    assert s._items[0].note.startswith("unverified")


def test_add_flags_a_listing_without_a_plain_add_button():
    s = _svc(web=_Web(listings={"B0C1C64R8S": MIC}))
    out = s.add([{"name": "mic", "asin": "B0C1C64R8S"}])
    assert "[the page has no plain Add-to-Cart button]" in out


def test_add_mentions_a_listing_title_that_reads_differently_from_the_label():
    s = _svc(web=_Web(listings={"B0C49RZ9WJ": SPEAKER}))
    out = s.add([{"name": "night vision lens", "asin": "B0C49RZ9WJ"}])
    assert "listing reads '8Pcs 8 Ohm 2W Speaker 28mm'" in out
    out2 = _svc(web=_Web(listings={"B0C49RZ9WJ": SPEAKER})).add([{"name": "28mm speaker", "asin": "B0C49RZ9WJ"}])
    assert "listing reads" not in out2


# ----- persistence + the face -----
def test_the_staged_list_survives_a_restart_and_the_face_hears_every_change():
    store = _Store()
    bus = SignalBus()
    snaps: list[dict] = []
    bus.subscribe(CartChanged, lambda ev: snaps.append(ev.snapshot))
    a = _svc(store=store, bus=bus, web=_Web(listings={"B0C49RZ9WJ": SPEAKER}))
    a.add([{"name": "speakers", "asin": "B0C49RZ9WJ", "quantity": 2}])
    assert snaps[-1]["items"][0]["quantity"] == 2 and snaps[-1]["estimated_total"] == 13.98
    assert snaps[-1]["items"][0]["image"].endswith("x.jpg") and snaps[-1]["driver"] is False
    b = _svc(store=store)  # a new process over the same file
    assert "speakers — quantity 2 (ASIN B0C49RZ9WJ) — about $6.99 each" in b.show()
    assert b.set_quantity("B0C49RZ9WJ", 5).startswith("speakers: quantity now 5")
    assert "Took out speakers" in b.set_quantity("B0C49RZ9WJ", 0)
    assert store.d["staged"] == []


def test_snapshot_shape_is_what_the_panel_draws():
    s = _svc(store=_Store())
    snap = s.snapshot()
    assert snap == {"items": [], "count": 0, "estimated_total": None, "unpriced": 0,
                    "driver": False, "opening": False, "last_handoff": None}


# ----- the handoff: drive Chrome, read the cart back -----
def _handoff_rig(*, parts=None, store=None):
    state = CartState(rows=(CartRow("B0C49RZ9WJ", 2, 6.99, "8Pcs Speaker"),), subtotal="$13.98",
                      account="Hello, sign in", url="https://www.amazon.com/gp/cart/view.html")
    driver = _Driver(results=[
        AddResult("B0C49RZ9WJ", "speakers", 2, 2, True, title="8Pcs Speaker"),
        AddResult("B0DCJR8JTG", "filament", 1, 0, False, reason="needs-option", title="PLA"),
    ], state=state)
    s = _svc(driver=driver, parts=parts, store=store or _Store(),
             web=_Web(listings={"B0C49RZ9WJ": SPEAKER, "B0DCJR8JTG": Listing("B0DCJR8JTG", "PLA", 9.99)}))
    s.add([{"name": "speakers", "asin": "B0C49RZ9WJ", "quantity": 2}, {"name": "filament", "asin": "B0DCJR8JTG"}])
    return s, driver


def test_open_cart_drives_the_window_and_reports_exactly_what_landed():
    s, driver = _handoff_rig()
    progress: list[str] = []
    out = s.open_cart(on_progress=progress.append)
    assert driver.calls == [("add", [("B0C49RZ9WJ", 2), ("B0DCJR8JTG", 1)])]
    assert "Amazon's cart is open in HELIX's own browser window with 1 of 2 products added" in out
    assert "Amazon's subtotal reads $13.98" in out
    assert "speakers — added 2 (Amazon's cart now holds 2)" in out
    assert "filament — NOT added: the listing needs an option picked" in out
    assert "isn't signed in to Amazon yet" in out and "Nothing has been purchased" in out
    assert progress == ["adding…"]
    # The added item left the staged list; the miss stays, with its reason on the panel.
    assert [i.asin for i in s._items] == ["B0DCJR8JTG"]
    assert s._items[0].note == "not added: the listing needs an option picked (size/color/pack) first"
    assert s.snapshot()["last_handoff"] == {"at": "2026-09-04T18:00", "how": "chrome", "count": 1,
                                            "subtotal": "$13.98"}


def test_a_driver_failure_falls_back_to_the_link_and_says_why():
    opened: list[str] = []
    s = _svc(driver=_Driver(raise_exc=ChromeCartError("no Chrome found")),
             opener=lambda url: opened.append(url) or True, store=_Store())
    s.add([{"name": "screws", "asin": "B08N5WRWNW"}])
    out = s.open_cart()
    assert len(opened) == 1 and "ASIN.1=B08N5WRWNW" in opened[0]
    assert "couldn't be driven: no Chrome found" in out
    assert "sign-in page first" in out and "resend_last" in out
    assert s._items == []
    assert "Re-sent the last cart link (1 item(s))" in s.open_cart(resend_last=True)
    assert len(opened) == 2


def test_no_driver_at_all_means_the_plain_link():
    opened: list[str] = []
    s = _svc(opener=lambda url: opened.append(url) or True)
    s.add([{"name": "screws", "asin": "B08N5WRWNW"}])
    out = s.open_cart()
    assert len(opened) == 1 and "couldn't be driven" not in out and "Nothing has been purchased" in out
    assert "no earlier link handoff" in _svc().open_cart(resend_last=True)


# ----- the parts-list bridge -----
def _parts():
    return PartsService(_Store(), clock=lambda: "2026-09-04T18:00")


def test_stage_parts_stages_needed_rows_at_planned_quantities_and_names_the_rest():
    parts = _parts()
    parts.save("IronEye", [
        {"name": "28mm speaker", "quantity": 2, "asin": "B0C49RZ9WJ"},
        {"name": "LiPo battery", "quantity": 1, "spec": "3.7V 500mAh"},
        {"name": "ESP32", "status": "on hand", "asin": "B08N5WRWNW"},
    ])
    s = _svc(parts=parts, web=_Web(listings={"B0C49RZ9WJ": SPEAKER}))
    out = s.stage_parts("iron eye")
    assert "Staged: 28mm speaker x2 at $6.99 (live)" in out
    assert "Still to resolve before they can be staged: LiPo battery (qty 1, 3.7V 500mAh)" in out
    assert "project='iron eye'" in out and "Left alone" in out and "ESP32" in out
    assert {r.name: r.status for r in parts.rows("IronEye")}["28mm speaker"] == "staged"
    assert s._items[0].project == "iron eye"
    assert "don't have a parts list called 'Nope'" in s.stage_parts("Nope")


def test_add_with_project_links_the_row_and_a_handoff_flips_it_to_carted_with_a_ledger_line():
    parts = _parts()
    parts.save("IronEye", [{"name": "28mm speaker", "quantity": 2}, {"name": "filament", "quantity": 1}])
    s, _ = _handoff_rig(parts=parts)
    s.remove("everything")
    s.add([{"name": "28mm speaker", "asin": "B0C49RZ9WJ", "quantity": 2},
           {"name": "filament", "asin": "B0DCJR8JTG"}], project="IronEye")
    rows = {r.name: r for r in parts.rows("IronEye")}
    assert rows["28mm speaker"].asin == "B0C49RZ9WJ" and rows["28mm speaker"].status == "staged"
    s.open_cart()
    rows = {r.name: r for r in parts.rows("IronEye")}
    assert rows["28mm speaker"].status == "carted" and rows["filament"].status == "staged"
    led = parts.ledger("IronEye")
    assert len(led) == 1 and led[0]["est_total"] == 13.98 and led[0]["how"].startswith("chrome")
    assert "Handed to Amazon" in parts.show("IronEye")
    # Removing a staged line gives the row back to "needed".
    s.remove("filament")
    assert {r.name: r.status for r in parts.rows("IronEye")}["filament"] == "need"


# ----- the live cart read -----
def test_check_amazon_cart_reads_the_real_cart_or_explains():
    state = CartState(rows=(CartRow("B0C49RZ9WJ", 2, 6.99, "8Pcs Speaker"),), subtotal="$13.98",
                      account="Hello, Brian")
    s = _svc(driver=_Driver(state=state))
    text = s.check_amazon_cart()
    assert "Amazon's cart holds 1 product, subtotal $13.98" in text
    assert "- 8Pcs Speaker — quantity 2 at $6.99 (ASIN B0C49RZ9WJ)" in text and "guest" not in text
    assert "can't read the live Amazon cart" in _svc().check_amazon_cart()
    assert "couldn't read the Amazon cart" in _svc(driver=_Driver(raise_exc=ChromeCartError("dead"))).check_amazon_cart()
    empty = _svc(driver=_Driver(state=CartState(rows=(), account="Hello, sign in"))).check_amazon_cart()
    assert "is empty" in empty and "guest cart" in empty


# ----- fences + registry -----
def test_the_new_writes_and_the_window_raise_are_fenced_and_the_reads_are_not():
    assert {"check_amazon_cart", "stage_parts", "save_parts", "remove_parts", "open_cart"} <= BUILD_TOOLS
    assert not ({"search_amazon", "lookup_amazon", "show_parts", "show_cart"} & BUILD_TOOLS)


def test_readable_texts_never_coach_a_fenced_tool():
    s = _svc(web=_Web(results=RESULTS, listings={"B0C49RZ9WJ": SPEAKER}), store=_Store())
    parts = _parts()
    parts.save("P", [{"name": "x"}])
    for text in (s.search("mic"), s.lookup("B0C49RZ9WJ"), s.show(), parts.show(), s.search("mic", budget=1)):
        for fenced in ("open_cart", "add_to_cart", "stage_parts", "check_amazon_cart", "save_parts", "remove_parts"):
            assert fenced not in text, (fenced, text)


def test_registry_offers_and_dispatches_the_amazon_and_parts_tools():
    calls: list = []

    class _Shop:
        def search(self, q, *, budget=None):
            calls.append(("search", q, budget)); return "S"

        def lookup(self, a):
            calls.append(("lookup", a)); return "L"

        def add(self, items, *, project=""):
            calls.append(("add", items, project)); return "A"

        def remove(self, w):
            return "R"

        def show(self):
            return "SH"

        def open_cart(self, *, resend_last=False, on_progress=None):
            calls.append(("open", resend_last, on_progress is not None)); return "O"

        def check_amazon_cart(self):
            return "C"

        def stage_parts(self, p):
            calls.append(("stage_parts", p)); return "SP"

    class _Parts:
        def save(self, p, items):
            calls.append(("save", p, items)); return "PS"

        def show(self, p=""):
            return "PSH"

        def remove(self, p, w):
            return "PR"

    reg = ToolRegistry(forge=None, builds=None, shopping=_Shop(), parts=_Parts())
    names = {s.name for s in reg.specs()}
    assert {"search_amazon", "lookup_amazon", "add_to_cart", "remove_from_cart", "show_cart", "open_cart",
            "check_amazon_cart", "save_parts", "show_parts", "remove_parts", "stage_parts"} <= names
    assert reg.dispatch("search_amazon", {"query": "mic", "budget": 10}) == "S"
    assert reg.dispatch("lookup_amazon", {"asin": "B0C49RZ9WJ"}) == "L"
    assert reg.dispatch("add_to_cart", {"items": [{"name": "x", "asin": "y"}], "project": "P"}) == "A"
    assert reg.dispatch("open_cart", {"resend_last": True}, on_progress=lambda s: None) == "O"
    assert reg.dispatch("check_amazon_cart", {}) == "C"
    assert reg.dispatch("save_parts", {"project": "P", "items": [{"name": "x"}]}) == "PS"
    assert reg.dispatch("show_parts", {}) == "PSH" and reg.dispatch("remove_parts", {"project": "P", "which": "x"}) == "PR"
    assert reg.dispatch("stage_parts", {"project": "P"}) == "SP"
    assert ("search", "mic", 10) in calls and ("add", [{"name": "x", "asin": "y"}], "P") in calls
    assert ("open", True, True) in calls and ("stage_parts", "P") in calls
    bare = {s.name for s in ToolRegistry(forge=None, builds=None).specs()}
    assert not ({"search_amazon", "save_parts", "stage_parts"} & bare)


# ----- the web shell -----
def test_shell_pushes_cart_events_and_product_cards_and_snapshots_the_cart():
    from tests.test_webshell import _Container
    from helix.api.shell import ShellSession

    container = _Container()
    container.shopping = _svc(store=_Store(), bus=container.bus, web=_Web(results=RESULTS, listings={"B0C1C64R8S": Listing("B0C1C64R8S", "mic", 11.69)}))
    events: list[dict] = []
    sh = ShellSession(container, events.append, voice=None)
    try:
        container.shopping.search("mic")
        cards = [e for e in events if e.get("t") == "msg" and e.get("visuals")]
        assert cards and cards[-1]["visuals"][0]["type"] == "products"
        assert cards[-1]["visuals"][0]["items"][0]["asin"] == "B0C1C64R8S"
        res = sh.cart_stage("B0C1C64R8S", "mic", 11.69, 2)
        assert res["ok"]
        cart_events = [e for e in events if e.get("t") == "cart"]
        assert cart_events and cart_events[-1]["cart"]["items"][0]["quantity"] == 2
        assert sh.snapshot()["cart"]["count"] == 2
        assert sh.cart_quantity("B0C1C64R8S", 3)["ok"] and sh.snapshot()["cart"]["count"] == 3
        assert sh.cart_remove("B0C1C64R8S")["ok"] and sh.snapshot()["cart"]["items"] == []
        assert sh.cart_open()["text"].startswith("The staged cart is empty")
        assert [e for e in events if e.get("t") == "msg"][-1]["text"].startswith("The staged cart is empty")
    finally:
        sh.shutdown()


def test_shell_without_a_shopping_service_reports_no_cart():
    from tests.test_webshell import _Container
    from helix.api.shell import ShellSession

    events: list[dict] = []
    sh = ShellSession(_Container(), events.append, voice=None)
    try:
        assert sh.snapshot()["cart"] is None
        assert sh.cart_open()["ok"] is False and sh.cart_stage("x", "y", None, 1)["ok"] is False
    finally:
        sh.shutdown()
