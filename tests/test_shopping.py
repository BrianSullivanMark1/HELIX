"""The Amazon cart faculty: verified-ASIN staging, the additive add-to-cart URL, the human-only
fence, and the one hard rule — HELIX stages and opens, the user reviews and buys."""
from __future__ import annotations

import threading

from helix.domain.shopping import (
    CART_BASE,
    MAX_ITEMS,
    MAX_QUANTITY,
    CartItem,
    cart_url,
    clamp_quantity,
    extract_asin,
    normalize_asin,
    price_summary,
    read_price,
)
from helix.services.shopping import ShoppingService


# ----- domain: ASIN reading (never guessing) -----
def test_normalize_asin_accepts_the_shape_and_forgives_case():
    assert normalize_asin("b08n5wrwnw") == "B08N5WRWNW"
    assert normalize_asin(" B0C1234XYZ ") == "B0C1234XYZ"
    assert normalize_asin("0136091814") == "0136091814"  # a book's ISBN-10 is its ASIN


def test_normalize_asin_rejects_everything_else():
    for bad in ("", "B08N5WRWN", "B08N5WRWNW9", "B08N5-RWNW", "add to cart", None):
        assert normalize_asin(bad or "") is None, bad


def test_extract_asin_reads_product_links_not_guesses():
    assert extract_asin("https://www.amazon.com/dp/B08N5WRWNW") == "B08N5WRWNW"
    assert extract_asin("https://www.amazon.com/Some-Product-Name/dp/B08N5WRWNW/ref=sr_1_3?keywords=x") \
        == "B08N5WRWNW"
    assert extract_asin("https://www.amazon.com/gp/product/b00exampl1?th=1") == "B00EXAMPL1"
    assert extract_asin("https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B08N5WRWNW&Quantity.1=2") \
        == "B08N5WRWNW"


def test_extract_asin_returns_none_for_lookalikes():
    # An 11-character token must never half-match; prose must never yield an id.
    assert extract_asin("https://www.amazon.com/dp/B08N5WRWNW9") is None
    assert extract_asin("the best m3 screws on amazon dot com") is None
    assert extract_asin("https://www.amazon.com/s?k=m3+screws") is None


def test_extract_asin_tolerates_pasted_link_punctuation():
    # Links arrive wrapped in prose: sentence periods, parens, commas, percent-encoded queries.
    assert extract_asin("https://www.amazon.com/dp/B08N5WRWNW.") == "B08N5WRWNW"
    assert extract_asin("(https://www.amazon.com/dp/B08N5WRWNW)") == "B08N5WRWNW"
    assert extract_asin("https://www.amazon.com/dp/B08N5WRWNW, please") == "B08N5WRWNW"
    assert extract_asin("https://www.amazon.com/Name/dp/B08N5WRWNW%3Fth%3D1") == "B08N5WRWNW"


def test_extract_asin_trusts_only_amazon_hosts():
    # Amazon-SHAPED is not Amazon-HOSTED: another storefront's /dp/ or /product/ path, or a
    # userinfo trick, must never mint a fabricated ASIN (never a guess).
    assert extract_asin("https://store.example.com/product/ABCDEFGH12?ref=1") is None
    assert extract_asin("https://shop.example.com/product/categories") is None
    assert extract_asin("https://evil.com/dp/B08N5WRWNW") is None
    assert extract_asin("https://evil-amazon.com/dp/B08N5WRWNW") is None
    assert extract_asin("https://amazon.com@evil.com/dp/B08N5WRWNW") is None
    # …while every real Amazon shape still reads.
    assert extract_asin("https://smile.amazon.co.uk/gp/product/B08N5WRWNW") == "B08N5WRWNW"
    assert extract_asin("amazon.de/dp/B08N5WRWNW") == "B08N5WRWNW"


def test_quantity_clamps_to_a_sane_range():
    assert clamp_quantity(3) == 3
    assert clamp_quantity("2") == 2
    assert clamp_quantity(0) == 1
    assert clamp_quantity(-5) == 1
    assert clamp_quantity(10_000) == MAX_QUANTITY
    assert clamp_quantity("a few") == 1  # unreadable means 1 — the list is read back before opening


def test_quantity_survives_the_infinities():
    # json.loads('1e400') is float('inf'); int(inf) raises OverflowError — that must mean 1, not a
    # mid-batch abort that leaves add()'s all-entries-reported contract broken.
    assert clamp_quantity(float("inf")) == 1
    assert clamp_quantity(float("-inf")) == 1
    assert clamp_quantity(float("nan")) == 1
    assert clamp_quantity("inf") == 1
    assert clamp_quantity(10**400) == 1


# ----- domain: prices (estimates for spoken answers, never authoritative) -----
def test_read_price_accepts_the_shapes_a_model_reads_off_a_page():
    assert read_price(12.99) == 12.99
    assert read_price("12.99") == 12.99
    assert read_price("$1,299.00") == 1299.0
    assert read_price(" $5 ") == 5.0


def test_read_price_refuses_garbage_rather_than_guessing():
    for bad in (None, "", "free", 0, -5, float("inf"), float("nan"), 10**400, 1e9):
        assert read_price(bad) is None, bad


def test_read_price_refuses_european_and_literal_shapes_instead_of_misreading():
    # "12,99" comma-stripped would stage EUR 12.99 as $1,299.00 — a confidently wrong number
    # spoken as money. Refusing beats guessing, for every non-US-money string shape.
    for bad in ("12,99", "1.299,00", "1,2,3", ",5", "1e3", "$1e3", "1_299.00"):
        assert read_price(bad) is None, bad
    assert read_price(True) is None      # json true is not a one-dollar price
    assert read_price(0.004) == None     # sub-cent rounds to $0.00 → refused, never 'costs nothing'
    assert read_price("12,345.67") == 12345.67  # real US thousands grouping still reads fine


def test_price_summary_totals_and_is_honest_about_gaps():
    items = [CartItem("a", "B000000001", 2, 10.0), CartItem("b", "B000000002", 1, 5.5)]
    line = price_summary(items)
    assert "$25.50" in line and "live truth" in line
    items.append(CartItem("c", "B000000003", 1, None))  # no price read for this one
    assert "plus 1 item without a price read" in price_summary(items)
    assert price_summary([CartItem("d", "B000000004", 1, None)]) == ""  # nothing priced → no line


# ----- domain: the cart URL -----
def test_cart_url_indexes_every_item_from_one():
    url = cart_url([CartItem("screws", "B08N5WRWNW", 2), CartItem("iron", "B0C1234XYZ", 1)])
    assert url.startswith(CART_BASE + "?")
    assert "ASIN.1=B08N5WRWNW" in url and "Quantity.1=2" in url
    assert "ASIN.2=B0C1234XYZ" in url and "Quantity.2=1" in url
    assert "ASIN.3" not in url


# ----- service: staging -----
def test_add_stages_and_reads_the_cart_back():
    s = ShoppingService(opener=lambda url: True)
    out = s.add([{"name": "M3 screws", "asin": "B08N5WRWNW", "quantity": 2}])
    assert "M3 screws x2" in out and "1 product" in out and "2 items" in out


def test_add_accepts_a_pasted_amazon_link_as_the_asin():
    s = ShoppingService(opener=lambda url: True)
    s.add([{"name": "filters", "asin": "https://www.amazon.com/dp/B0C1234XYZ?ref=x"}])
    assert "ASIN B0C1234XYZ" in s.show()


def test_add_refuses_a_fake_asin_by_name_and_tells_the_model_not_to_guess():
    s = ShoppingService(opener=lambda url: True)
    out = s.add([{"name": "mystery widget", "asin": "not-an-asin"}])
    assert "Couldn't stage" in out and "mystery widget" in out
    assert "never guess" in out.lower()
    assert "empty" in s.show().lower() or "empty" in out.lower()  # nothing slipped through


def test_add_merges_the_same_asin_as_more_of_it():
    s = ShoppingService(opener=lambda url: True)
    s.add([{"name": "screws", "asin": "B08N5WRWNW", "quantity": 1}])
    out = s.add([{"name": "screws again", "asin": "b08n5wrwnw", "quantity": 2}])
    assert "now 3" in out
    assert "quantity 3" in s.show()


def test_staged_prices_power_the_spoken_recap_and_total():
    s = ShoppingService(opener=lambda url: True)
    s.add([{"name": "screws", "asin": "B08N5WRWNW", "quantity": 2, "price": 12.99},
           {"name": "iron", "asin": "B0C1234XYZ", "price": "$24.50"}])
    recap = s.show()
    assert "about $12.99 each" in recap and "about $24.50 each" in recap
    assert "Estimated total about $50.48" in recap  # 2×12.99 + 24.50
    # A fresh read on a re-stage replaces a stale price; a missing read keeps the old one.
    s.add([{"name": "screws", "asin": "B08N5WRWNW", "quantity": 1, "price": 11.99}])
    assert "about $11.99 each" in s.show()
    s.add([{"name": "screws", "asin": "B08N5WRWNW", "quantity": 1}])
    assert "about $11.99 each" in s.show()  # no price passed → the last real read survives


def test_unpriced_items_never_break_money_answers():
    s = ShoppingService(opener=lambda url: True)
    s.add([{"name": "mystery", "asin": "B000000005", "price": "call for pricing"}])
    recap = s.show()
    assert "each" not in recap and "Estimated total" not in recap  # bad read refused, no fake math
    # And the recap SAYS no prices were read — the anchor that keeps a spoken "what's the total?"
    # answer honest instead of pattern-matched from the persona's priced example.
    assert "No prices were read" in recap


def test_add_caps_the_staged_list_honestly():
    s = ShoppingService(opener=lambda url: True)
    items = [{"name": f"item {i}", "asin": f"B{i:09d}"} for i in range(MAX_ITEMS)]
    s.add(items)
    out = s.add([{"name": "one too many", "asin": "B999999999"}])
    assert "full" in out and "one too many" in out


def test_add_with_nothing_says_what_it_needs():
    s = ShoppingService(opener=lambda url: True)
    assert "ASIN" in s.add([])
    assert "ASIN" in s.add("not a list")


# ----- service: removing -----
def test_remove_by_name_asin_and_everything():
    s = ShoppingService(opener=lambda url: True)
    s.add([{"name": "M3 screws", "asin": "B08N5WRWNW"}, {"name": "filters", "asin": "B0C1234XYZ"}])
    out = s.remove("filters")
    assert "Took out filters" in out
    out = s.remove("b08n5wrwnw")  # by ASIN, case-forgiven
    assert "M3 screws" in out and "empty" in out.lower()
    s.add([{"name": "a", "asin": "B000000001"}, {"name": "b", "asin": "B000000002"}])
    assert "Cleared" in s.remove("everything")
    assert "empty" in s.show().lower()


def test_remove_with_no_match_is_honest():
    s = ShoppingService(opener=lambda url: True)
    s.add([{"name": "screws", "asin": "B08N5WRWNW"}])
    out = s.remove("plutonium")
    assert "Nothing staged matches" in out
    assert "quantity 1" in s.show()  # untouched


# ----- service: the one outward act -----
def test_open_cart_opens_the_exact_url_and_clears_the_staged_list():
    opened: list[str] = []
    s = ShoppingService(opener=lambda url: opened.append(url) or True)
    s.add([{"name": "screws", "asin": "B08N5WRWNW", "quantity": 2},
           {"name": "iron", "asin": "B0C1234XYZ"}])
    out = s.open_cart()
    assert opened == [cart_url([CartItem("screws", "B08N5WRWNW", 2), CartItem("iron", "B0C1234XYZ", 1)])]
    # The return teaches the model the boundary: pre-loaded, reviewed and bought by the USER.
    assert "Nothing has been purchased" in out and "check out themselves" in out
    assert "empty" in s.show().lower()  # cleared — Amazon's link is additive; no double-adds later


def test_open_cart_refuses_an_empty_cart_without_touching_the_browser():
    opened: list[str] = []
    s = ShoppingService(opener=lambda url: opened.append(url) or True)
    assert "empty" in s.open_cart().lower()
    assert opened == []


def test_open_cart_keeps_the_staged_list_when_the_browser_fails():
    s = ShoppingService(opener=lambda url: False)
    s.add([{"name": "screws", "asin": "B08N5WRWNW"}])
    assert "couldn't open" in s.open_cart().lower()
    assert "quantity 1" in s.show()  # still staged — the user can say go again

    def _boom(url):
        raise OSError("no browser")

    s2 = ShoppingService(opener=_boom)
    s2.add([{"name": "screws", "asin": "B08N5WRWNW"}])
    assert "couldn't open" in s2.open_cart().lower()  # an exception must never crash the turn
    assert "quantity 1" in s2.show()


def test_open_cart_never_holds_the_lock_across_the_browser_launch():
    # A BROWSER-env launcher can block for the browser's lifetime. While the opener runs, the
    # service must stay responsive (show works, edits wait politely) instead of wedging every
    # cart tool behind a held lock — pinned by an opener that re-enters the service, which would
    # deadlock forever if the lock were held across the launch.
    seen: dict = {}

    def _reentrant(url):
        svc = seen["svc"]
        seen["show"] = svc.show()                                     # reads fine mid-open
        seen["add"] = svc.add([{"name": "late", "asin": "B000000009"}])  # edits wait their turn
        seen["again"] = svc.open_cart()                               # a second open is refused
        return True

    s = ShoppingService(opener=_reentrant)
    seen["svc"] = s
    s.add([{"name": "screws", "asin": "B08N5WRWNW"}])
    out = s.open_cart()
    assert "Nothing has been purchased" in out
    assert "screws" in seen["show"]              # the staged list was intact during the handover
    assert "Hold on" in seen["add"]              # no silent mid-handover mutation…
    assert "already opening" in seen["again"]    # …and no double-fire double-add
    assert "empty" in s.show().lower()           # cleared exactly once, after success


def test_cart_edits_are_thread_safe():
    s = ShoppingService(opener=lambda url: True)

    def _stage(i: int) -> None:
        s.add([{"name": f"item {i}", "asin": f"B{i:09d}"}])

    threads = [threading.Thread(target=_stage, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.show().count("ASIN ") == 12  # every concurrent add landed exactly once


# ----- registry + fence + persona -----
def test_registry_exposes_and_dispatches_the_cart_tools():
    from helix.services.tools import ToolRegistry

    opened: list[str] = []
    svc = ShoppingService(opener=lambda url: opened.append(url) or True)
    reg = ToolRegistry(forge=None, builds=None, shopping=svc)
    names = {t.name for t in reg.specs()}
    assert {"add_to_cart", "remove_from_cart", "show_cart", "open_cart"} <= names
    out = reg.dispatch("add_to_cart", {"items": [{"name": "screws", "asin": "B08N5WRWNW"}]})
    assert "screws" in out
    assert "screws" in reg.dispatch("show_cart", {})
    assert "Took out" in reg.dispatch("remove_from_cart", {"which": "screws"})
    reg.dispatch("add_to_cart", {"items": [{"name": "iron", "asin": "B0C1234XYZ"}]})
    assert "Nothing has been purchased" in reg.dispatch("open_cart", {})
    assert len(opened) == 1


def test_registry_without_a_shopping_service_hides_the_tools():
    from helix.services.tools import ToolRegistry

    names = {t.name for t in ToolRegistry(forge=None, builds=None).specs()}
    assert not ({"add_to_cart", "remove_from_cart", "show_cart", "open_cart"} & names)


def test_cart_mutations_are_fenced_off_autonomous_runs():
    from helix.services.conversation import BUILD_TOOLS

    # An unattended watcher processing an email must never stage merchandise or pop a browser.
    assert {"add_to_cart", "remove_from_cart", "open_cart"} <= BUILD_TOOLS
    assert "show_cart" not in BUILD_TOOLS  # the recap stays readable, like list_reminders


def test_show_cart_text_never_coaches_a_fenced_tool():
    # show_cart is readable on autonomous runs, so its RETURN text must describe, never command —
    # a recap that says "call open_cart" would coach an unattended model straight at the fence.
    s = ShoppingService(opener=lambda url: True)
    for text in (s.show(), s.add([{"name": "screws", "asin": "B08N5WRWNW"}]), s.show()):
        assert "open_cart" not in text and "add_to_cart" not in text


def test_dispatch_refuses_a_tool_the_run_was_not_offered():
    # The fence holds at DISPATCH, not just at offer time: an autonomous run whose model emits a
    # fenced tool_use name anyway (e.g. coached by text it read) gets an error result — the tool
    # itself is never touched. This pins the class-level guard, not just the cart instance.
    from datetime import datetime

    from helix.ports.llm import Reply, Text, ToolSpec, ToolUse, Usage
    from helix.services.conversation import ConversationService

    class _PushyChat:
        def __init__(self):
            self.calls = 0

        def chat(self, turns, *, system=None, tools=None):
            self.calls += 1
            if self.calls == 1:  # try the fenced tool despite it not being offered
                return Reply(blocks=(ToolUse("t1", "open_cart", {}),), usage=Usage())
            return Reply(blocks=(Text("done"),), usage=Usage())

    class _Store:
        def append(self, m): pass
        def recent(self, limit=100): return []

    class _Memory:
        def record_usage(self, *a): pass

    class _Clock:
        def now(self): return datetime(2026, 8, 1, 12, 0, 0)

    opened: list[str] = []
    shopping = ShoppingService(opener=lambda url: opened.append(url) or True)
    shopping.add([{"name": "screws", "asin": "B08N5WRWNW"}])

    class _Tools:
        def __init__(self):
            self.dispatched: list[str] = []

        def specs(self):
            return [ToolSpec("show_cart", "recap", {"type": "object", "properties": {}}),
                    ToolSpec("open_cart", "open", {"type": "object", "properties": {}})]

        def dispatch(self, name, args, **k):
            self.dispatched.append(name)
            return shopping.open_cart() if name == "open_cart" else shopping.show()

    tools = _Tools()
    svc = ConversationService(_PushyChat(), tools, _Store(), _Memory(), _Clock(), "sys")
    svc.run_turn("watch things", allow_builds=False, persist=False)  # open_cart is BUILD_TOOLS-fenced
    assert tools.dispatched == []   # never dispatched…
    assert opened == []             # …so no browser ever opened on an autonomous run
    assert "quantity 1" in shopping.show()  # and the staged list was never touched


def test_persona_teaches_the_flow_and_the_no_guessing_rule():
    from helix.services.prompts import CONSOLE_SYSTEM

    flat = " ".join(CONSOLE_SYSTEM.split())
    for tool in ("search_amazon", "lookup_amazon", "add_to_cart", "open_cart", "remove_from_cart",
                 "check_amazon_cart", "save_parts", "stage_parts", "show_parts"):
        assert tool in flat, tool
    # HELIX searches; it never sends the user to search, never asks permission it already has,
    # and never recalls an id — the exact failures of the September 4 session.
    assert "SEARCH AND STAGE IN THE SAME TURN" in flat
    assert "Never tell the user what to type into Amazon" in flat
    assert "never recall an ASIN from memory" in flat
    # The three states are named apart, so "it's in your cart" can't be said of a staged line.
    assert "three different things" in flat
    # The boundary: HELIX stages and hands over, the user buys — and the window's guest-cart truth.
    assert "only they check out" in flat and "guest cart" in flat
    # The composition with sight: a listing screenshot, a part on the camera.
    assert "read its title off the picture" in flat and "view_camera, read its markings" in flat
    # Parts lists are the durable BOM, staged whole at planned quantities.
    assert "stage_parts (needed rows at planned quantities)" in flat


def test_cart_tools_have_speakable_labels():
    from helix.domain.vocabulary import friendly_tool_label

    for tool in ("add_to_cart", "remove_from_cart", "show_cart", "open_cart"):
        label = friendly_tool_label(tool)
        assert label != "Working…" and "_" not in label, tool
