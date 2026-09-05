"""HELIX's own eyes on Amazon: the search-page and product-page parsers (against markup shaped like
the live pages), the model-facing text, and the paced/cached/Amazon-only fetcher."""
from __future__ import annotations

import urllib.request

import pytest

from helix.adapters.amazon_web import AmazonUnavailable, AmazonWeb, _AmazonOnlyRedirects
from helix.domain.amazon import (
    format_listing,
    format_products,
    is_blocked,
    parse_listing,
    parse_search,
)


def _card(asin: str, title: str, *, price: str | None = "$9.99", sponsored: bool = False,
          rating: str = "4.6", reviews: str = "1,234", prime: bool = True, list_price: str = "",
          unit: str = "", bought: str = "") -> str:
    parts = [f'<div data-component-type="s-search-result" data-asin="{asin}" class="s-result-item">']
    if sponsored:
        parts.append('<span class="puis-sponsored-label-text">Sponsored</span>')
    parts.append(f'<h2 class="a-size-base"><a class="a-link-normal s-no-outline" href="/Some-Name/dp/{asin}/ref=sr_1_1">'
                 f'<span>{title}</span></a></h2>')
    parts.append(f'<a aria-label="{rating} out of 5 stars, rating details"><span class="a-icon-alt">'
                 f'{rating} out of 5 stars</span></a>')
    parts.append(f'<a aria-label="{reviews} ratings"><span class="a-size-base s-underline-text">{reviews}</span></a>')
    if bought:
        parts.append(f'<span class="a-size-base a-color-secondary">{bought} bought in past month</span>')
    if price is not None:
        parts.append(f'<span class="a-price" data-a-color="base"><span class="a-offscreen">{price}</span></span>')
        if unit:
            parts.append(f'<span class="a-size-base a-color-secondary">({unit})</span>')
    if list_price:
        parts.append(f'<span class="a-price a-text-price"><span class="a-offscreen">{list_price}</span></span>')
    if prime:
        parts.append('<i class="a-icon a-icon-prime" aria-label="Amazon Prime"></i>')
    parts.append(f'<img class="s-image" src="https://m.media-amazon.com/images/I/{asin}.jpg">')
    parts.append("</div>")
    return "".join(parts)


SEARCH_HTML = "<html><body>" + "".join([
    _card("B0FKFR1WFX", "3 PCS INMP441 Microphone Module", price="$8.99", bought="100+"),
    _card("B0SPONSORD", "Sponsored Mic", sponsored=True, price="$19.99"),
    _card("B0972XP1YS", "AITRIP 3PCS INMP441", price="$9.99", list_price="$14.99", unit="$3.33/Count",
          reviews="110"),
    _card("B0NOPRICE1", "Mystery Mic", price=None, prime=False, rating="4.0", reviews="7"),
    _card("B0FKFR1WFX", "duplicate card of the first", price="$1.00"),
    '<div data-component-type="s-search-result" data-asin="BADASIN">no title</div>',
]) + "</body></html>"

PRODUCT_HTML = """<html><head><title>Amazon.com: 8Pcs 8 Ohm 2W Speaker</title></head><body>
<span id="productTitle"> 8Pcs 8 Ohm 2W Speaker 8ohm Round 28mm </span>
<a id="bylineInfo">Visit the Shutao Store</a>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price a-text-price"><span class="a-offscreen">$9.99</span></span>
  <span class="a-price"><span class="a-offscreen">$6.99</span></span>
</div>
<div id="availability"><span> In Stock </span></div>
<span id="acrPopover" title="4.0 out of 5 stars"></span>
<span id="acrCustomerReviewText">34 ratings</span>
<div id="primeDeliveryBadge"></div>
<img id="landingImage" src="https://m.media-amazon.com/images/I/61Rw.jpg" data-old-hires="https://m.media-amazon.com/images/I/61Rw._SL1500_.jpg">
<div id="feature-bullets"><ul><li><span class="a-list-item"> 28mm speaker, 2W 8 ohm </span></li>
<li><span class="a-list-item">Easy to install</span></li></ul></div>
<table id="productDetails_techSpec_section_1"><tr><th>Item model number</th><td>SP-28</td></tr>
<tr><th>Impedance</th><td>8 Ohm</td></tr></table>
<div id="variation_color_name"><label class="a-form-label">Color:</label><span class="selection">Black</span></div>
<select id="quantity"><option value="1">1</option><option value="2">2</option><option value="30">30</option></select>
<input id="add-to-cart-button" type="submit">
</body></html>"""

ROBOT_HTML = "<html><title>Robot Check</title><body>Type the characters you see in this image</body></html>"


# ----- the search page -----
def test_parse_search_reads_the_cards_organic_first_and_dedupes():
    rows = parse_search(SEARCH_HTML)
    asins = [r.asin for r in rows]
    assert asins == ["B0FKFR1WFX", "B0972XP1YS", "B0NOPRICE1", "B0SPONSORD"]  # sponsored last, dupe dropped
    first = rows[0]
    assert first.title == "3 PCS INMP441 Microphone Module"
    assert first.price == 8.99 and first.rating == 4.6 and first.reviews == 1234
    assert first.prime and not first.sponsored and first.bought == "100+"
    assert first.image == "https://m.media-amazon.com/images/I/B0FKFR1WFX.jpg"
    assert first.url == "https://www.amazon.com/dp/B0FKFR1WFX"


def test_parse_search_skips_the_struck_list_price_and_reads_the_unit_price():
    rows = {r.asin: r for r in parse_search(SEARCH_HTML)}
    aitrip = rows["B0972XP1YS"]
    assert aitrip.price == 9.99            # the $14.99 list price is struck through, never the price
    assert aitrip.unit_price == "$3.33/Count"
    assert aitrip.reviews == 110
    assert rows["B0SPONSORD"].sponsored
    assert rows["B0NOPRICE1"].price is None and not rows["B0NOPRICE1"].prime


def test_parse_search_limit_and_empty_and_blocked():
    assert len(parse_search(SEARCH_HTML, limit=2)) == 2
    assert parse_search("") == []
    assert parse_search(ROBOT_HTML) == []
    assert is_blocked(ROBOT_HTML) and not is_blocked(SEARCH_HTML)


def test_format_products_reads_like_a_shopper_and_never_coaches_a_fenced_tool():
    text = format_products("inmp441", parse_search(SEARCH_HTML))
    assert "1. 3 PCS INMP441 Microphone Module — $8.99; 4.6 stars (1,234 ratings); Prime; 100+ bought last month — ASIN B0FKFR1WFX" in text
    assert "sponsored" in text and "price not shown" in text and "($3.33/Count)" in text
    # search_amazon is READABLE on autonomous runs, so its text must not name a fenced tool.
    for fenced in ("add_to_cart", "open_cart", "stage_parts", "check_amazon_cart"):
        assert fenced not in text
    assert "no product cards" in format_products("x", [])


# ----- the product page -----
def test_parse_listing_reads_the_fields_that_decide_the_part():
    l = parse_listing(PRODUCT_HTML, "b0c49rz9wj")
    assert l is not None
    assert l.asin == "B0C49RZ9WJ"
    assert l.title == "8Pcs 8 Ohm 2W Speaker 8ohm Round 28mm"
    assert l.price == 6.99                  # not the struck-through $9.99
    assert l.availability == "In Stock"
    assert l.rating == 4.0 and l.reviews == 34 and l.prime
    assert l.brand == "Shutao"
    assert l.image.endswith("_SL1500_.jpg")  # the hi-res one
    assert l.bullets == ("28mm speaker, 2W 8 ohm", "Easy to install")
    assert ("Item model number", "SP-28") in l.specs
    assert l.variations == "Color: Black"
    assert l.can_add and l.max_quantity == 30


def test_parse_listing_returns_none_for_non_product_pages():
    assert parse_listing("", "B0C49RZ9WJ") is None
    assert parse_listing(ROBOT_HTML, "B0C49RZ9WJ") is None
    assert parse_listing("<html><body>Page not found</body></html>", "B0C49RZ9WJ") is None
    # A page without a usable ASIN anywhere is not a listing either.
    assert parse_listing('<html><body><span id="productTitle">X</span></body></html>', "") is None
    # The page's own hidden ASIN input is read when no hint is given.
    html = '<html><body><span id="productTitle">X</span><input id="ASIN" value="B0C49RZ9WJ"></body></html>'
    assert parse_listing(html, "").asin == "B0C49RZ9WJ"


def test_format_listing_says_what_a_buyer_needs():
    text = format_listing(parse_listing(PRODUCT_HTML, "B0C49RZ9WJ"))
    assert text.startswith("8Pcs 8 Ohm 2W Speaker 8ohm Round 28mm (ASIN B0C49RZ9WJ)")
    assert "$6.99; 4.0 stars (34 ratings); Prime; by Shutao; In Stock" in text
    assert "Options on the page: Color: Black" in text
    assert "Item model number: SP-28" in text
    no_button = PRODUCT_HTML.replace('<input id="add-to-cart-button" type="submit">', "")
    assert "no plain Add-to-Cart button" in format_listing(parse_listing(no_button, "B0C49RZ9WJ"))


# ----- the fetcher -----
class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _web(pages: dict[str, str]):
    calls: list[str] = []
    slept: list[float] = []
    clock = _Clock()

    def fetch(url: str) -> str:
        calls.append(url)
        return pages.get(url, "")

    def sleep(s: float) -> None:
        slept.append(s)
        clock.t += s

    return AmazonWeb(fetch=fetch, clock=clock, sleep=sleep), calls, slept, clock


def test_search_and_listing_go_through_the_fetcher_and_parse():
    web, calls, _, _ = _web({
        "https://www.amazon.com/s?k=inmp441+i2s": SEARCH_HTML,
        "https://www.amazon.com/dp/B0C49RZ9WJ?th=1&psc=1": PRODUCT_HTML,
    })
    rows = web.search("  inmp441   i2s ", limit=3)
    assert [r.asin for r in rows] == ["B0FKFR1WFX", "B0972XP1YS", "B0NOPRICE1"]
    assert web.listing("B0C49RZ9WJ").price == 6.99
    assert web.listing("B0DEADDEAD") is None  # a 404 (empty body) is "no such product", not an error
    assert web.search("") == []


def test_reads_are_paced_and_cached():
    web, calls, slept, clock = _web({"https://www.amazon.com/s?k=x": SEARCH_HTML})
    web.search("x")
    web.search("x")                       # cached: no second fetch
    assert len(calls) == 1 and slept == []
    web._cache.clear()
    clock.t += 0.2                        # 0.2s after the last fetch → the pacer sleeps the rest
    web.search("x")
    assert len(calls) == 2 and slept and 0.9 < slept[0] < 1.3


def test_a_robot_wall_is_unavailable_not_an_empty_result():
    web, _, _, _ = _web({"https://www.amazon.com/s?k=x": ROBOT_HTML})
    with pytest.raises(AmazonUnavailable):
        web.search("x")


def test_redirects_stay_on_amazon():
    h = _AmazonOnlyRedirects()
    req = urllib.request.Request("https://www.amazon.com/dp/B0C49RZ9WJ")
    assert h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/dp/B0C49RZ9WJ") is None
    assert h.redirect_request(req, None, 302, "Found", {}, "https://amazon.com.evil.net/x") is None
    ok = h.redirect_request(req, None, 302, "Found", {}, "https://www.amazon.com/Some-Name/dp/B0C49RZ9WJ")
    assert ok is not None and ok.full_url.startswith("https://www.amazon.com/")
