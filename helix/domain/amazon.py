"""Amazon pages as data — HELIX's own eyes on the store. Pure parsing, no I/O.

The model used to "find" products through web-search snippets and recall ASINs from memory: ids
drifted between turns, prices were guessed, and the user ended up screenshotting listings one by one
and asking "is this the right part?". These parsers turn the two pages that matter — a SEARCH page
and a PRODUCT page — into plain rows the model can reason over and the face can draw as cards, so
HELIX searches Amazon itself, reads the live price off the listing, and stages a verified id.

Everything here is untrusted page content: titles and specs are TEXT the UI renders as text, never
markup; an ASIN is accepted only when it has the real shape. Amazon's markup changes; every extractor
is best-effort and degrades to None rather than raising, so one moved class costs a field, not a tool.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from helix.domain.shopping import MAX_PRICE, normalize_asin

# The signatures of Amazon's automation wall (a "Robot Check" page answers 200 with a CAPTCHA).
_BLOCK_MARKS = ("api-services-support@amazon.com", "Robot Check", "Type the characters you see")

_MONEY = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")
_RATING = re.compile(r"(\d(?:\.\d)?)\s+out of\s+5")
_COUNT = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)")
_RATINGS = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)\s+ratings?")
_BOUGHT = re.compile(r"(\d[\d,]*\+?[KkMm]?)\s*bought in past month", re.IGNORECASE)
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Product:
    """One search-result card."""

    asin: str
    title: str
    price: float | None = None
    rating: float | None = None       # 0..5
    reviews: int | None = None        # rating count
    prime: bool = False
    sponsored: bool = False
    image: str = ""                   # https://m.media-amazon.com/… (the face draws it)
    bought: str = ""                  # "2K+" — Amazon's "bought in past month" social proof
    unit_price: str = ""              # "$0.87/Count" when the card shows one

    @property
    def url(self) -> str:
        return f"https://www.amazon.com/dp/{self.asin}"


@dataclass(frozen=True)
class Listing:
    """A product page, read for the fields that decide "is this the right part?"."""

    asin: str
    title: str
    price: float | None = None
    availability: str = ""
    rating: float | None = None
    reviews: int | None = None
    prime: bool = False
    brand: str = ""
    image: str = ""
    bullets: tuple[str, ...] = ()
    specs: tuple[tuple[str, str], ...] = ()   # ("Item model number", "INMP441"), …
    variations: str = ""                       # "Color: Black; Size: 3-Pack" when the page has pickers
    can_add: bool = True                       # an Add-to-Cart button is on the page
    max_quantity: int | None = None            # the quantity picker's ceiling, when shown

    @property
    def url(self) -> str:
        return f"https://www.amazon.com/dp/{self.asin}"


def is_blocked(html: str) -> bool:
    """True when Amazon answered with its automation wall instead of a page."""
    head = (html or "")[:200_000]
    return any(mark in head for mark in _BLOCK_MARKS)


# ----- helpers -----
def _text(node) -> str:
    try:
        return _WS.sub(" ", node.text_content()).strip()
    except Exception:  # noqa: BLE001 — a comment node, a broken subtree
        return ""


def _first(node, *xpaths: str):
    """The first element any of `xpaths` finds, or None (explicit: lxml elements are falsy when
    childless, so `a or b` on elements is a trap)."""
    for xp in xpaths:
        try:
            hits = node.xpath(xp)
        except Exception:  # noqa: BLE001
            continue
        if hits:
            return hits[0]
    return None


def _ratings_count(text: str) -> int | None:
    m = _RATINGS.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _money(text: str) -> float | None:
    m = _MONEY.search(text or "")
    if not m:
        return None
    try:
        value = round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None
    return value if 0 < value <= MAX_PRICE else None


def _rating(text: str) -> float | None:
    m = _RATING.search(text or "")
    if not m:
        return None
    try:
        r = float(m.group(1))
    except ValueError:
        return None
    return r if 0 <= r <= 5 else None


def _count(text: str) -> int | None:
    m = _COUNT.search((text or "").replace("(", " ").replace(")", " "))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse(html: str):
    from lxml import html as lhtml  # imported lazily: the domain stays importable without lxml

    return lhtml.fromstring(html or "<html></html>")


# ----- the search page -----
def parse_search(html: str, *, limit: int = 12) -> list[Product]:
    """The result cards on an Amazon search page, organic first, sponsored after, deduplicated by
    ASIN. `limit` caps the list (the model reasons over a handful, not a page)."""
    if not html or is_blocked(html):
        return []
    doc = _parse(html)
    cards = doc.xpath('//div[@data-component-type="s-search-result"][@data-asin]')
    organic: list[Product] = []
    sponsored: list[Product] = []
    seen: set[str] = set()
    for card in cards:
        asin = normalize_asin(card.get("data-asin") or "")
        if asin is None or asin in seen:
            continue
        title_node = _first(card, './/h2//span[normalize-space()]', './/h2')
        title = _text(title_node) if title_node is not None else ""
        if not title:
            continue
        seen.add(asin)
        # The main price: the first .a-price that is NOT a struck-through list price. A card's
        # secondary "$0.87/Count" unit price sits in its own a-price with a-size-base wrapper text.
        price = None
        unit = ""
        for pn in card.xpath('.//span[contains(@class,"a-price") and not(contains(@class,"a-text-price"))]'):
            off = _first(pn, './/span[@class="a-offscreen"]')
            val = _money(_text(off) if off is not None else "")
            if val is None:
                continue
            if price is None:
                price = val
                parent = _first(pn, "..")
                tail = _text(parent if parent is not None else pn)
                m = re.search(r"\(\s*(\$[\d.,]+\s*/\s*[A-Za-z ]+)\)", tail)
                if m:
                    unit = _WS.sub(" ", m.group(1)).replace(" /", "/").replace("/ ", "/")
                break
        rating_node = _first(card, './/span[@class="a-icon-alt"]',
                             './/*[contains(@aria-label,"out of 5 stars")]')
        rating = None
        if rating_node is not None:
            rating = _rating(_text(rating_node)) or _rating(rating_node.get("aria-label", ""))
        reviews = None
        for rv in card.xpath('.//*[contains(@aria-label," rating")]'):
            reviews = _ratings_count(rv.get("aria-label", ""))
            if reviews is not None:
                break
        if reviews is None:
            rv = _first(card, './/span[contains(@class,"s-underline-text")]')
            reviews = _count(_text(rv)) if rv is not None else None
        # Prime: the badge icon for a member, or the "join Prime" delivery trigger Amazon shows a
        # non-member on Prime-eligible items — either way the item ships Prime.
        prime = bool(card.xpath('.//*[contains(@class,"a-icon-prime") or contains(@class,"-prime-")'
                                ' or @aria-label="Amazon Prime"]'))
        sponsored_flag = bool(card.xpath(
            './/*[contains(@class,"s-sponsored-label-text") or contains(@class,"puis-sponsored-label-text")]'
            ' | .//*[@data-component-type="sp-sponsored-result"]'))
        img = _first(card, './/img[contains(@class,"s-image")]')
        image = (img.get("src") or "").strip() if img is not None else ""
        if not image.startswith("https://"):
            image = ""
        bought = ""
        bn = _first(card, './/span[contains(text(),"bought in past month")]')
        m = _BOUGHT.search(_text(bn)) if bn is not None else None
        if m:
            bought = m.group(1)
        row = Product(asin=asin, title=title[:200], price=price, rating=rating, reviews=reviews,
                      prime=prime, sponsored=sponsored_flag, image=image, bought=bought,
                      unit_price=unit)
        (sponsored if sponsored_flag else organic).append(row)
    return (organic + sponsored)[:limit]


# ----- the product page -----
def parse_listing(html: str, asin_hint: str = "") -> Listing | None:
    """The fields of a product page, or None when the page is not a product page (a search, a
    robot check, a 'page not found')."""
    if not html or is_blocked(html):
        return None
    doc = _parse(html)
    title_node = _first(doc, '//*[@id="productTitle"]')
    title = _text(title_node) if title_node is not None else ""
    asin = normalize_asin(asin_hint)
    if asin is None:
        node = _first(doc, '//input[@id="ASIN"]', '//*[@data-asin][@id="dp"]')
        asin = normalize_asin((node.get("value") or node.get("data-asin") or "") if node is not None else "")
    if not title or asin is None:
        return None
    price = None
    for xp in (
        '//*[@id="corePriceDisplay_desktop_feature_div"]//span[contains(@class,"a-price") and not(contains(@class,"a-text-price"))]//span[@class="a-offscreen"]',
        '//*[@id="corePrice_feature_div"]//span[contains(@class,"a-price") and not(contains(@class,"a-text-price"))]//span[@class="a-offscreen"]',
        '//*[@id="apex_desktop"]//span[contains(@class,"a-price") and not(contains(@class,"a-text-price"))]//span[@class="a-offscreen"]',
        '//*[@id="tp_price_block_total_price_ww"]//span[@class="a-offscreen"]',
        '//*[@id="priceblock_ourprice"] | //*[@id="priceblock_dealprice"]',
    ):
        node = _first(doc, xp)
        if node is not None:
            price = _money(_text(node))
            if price is not None:
                break
    avail_node = _first(doc, '//*[@id="availability"]//span[normalize-space()]', '//*[@id="availability"]')
    availability = _text(avail_node)[:80] if avail_node is not None else ""
    rating = None
    rnode = _first(doc, '//*[@id="acrPopover"]')
    if rnode is not None:
        rating = _rating(rnode.get("title", "")) or _rating(_text(rnode))
    if rating is None:
        rnode = _first(doc, '//span[@data-hook="rating-out-of-text"]')
        rating = _rating(_text(rnode)) if rnode is not None else None
    reviews = None
    cnode = _first(doc, '//*[@id="acrCustomerReviewText"]')
    if cnode is not None:
        reviews = _ratings_count(_text(cnode)) or _count(_text(cnode))
    prime = bool(doc.xpath('//*[@id="primeDeliveryBadge"] | //*[@id="buybox"]//*[contains(@class,"a-icon-prime")]'
                           ' | //*[@id="deliveryBlockMessage"]//*[contains(@class,"prime")]'))
    bnode = _first(doc, '//*[@id="bylineInfo"]')
    brand = _text(bnode)[:60] if bnode is not None else ""
    brand = re.sub(r"^(Visit the|Brand:)\s*", "", brand).replace(" Store", "").strip()
    inode = _first(doc, '//img[@id="landingImage"]', '//*[@id="imgTagWrapperId"]//img')
    image = ""
    if inode is not None:
        image = (inode.get("data-old-hires") or inode.get("src") or "").strip()
        if not image.startswith("https://"):
            image = ""
    bullets = []
    for li in doc.xpath('//*[@id="feature-bullets"]//li//span[contains(@class,"a-list-item")]'):
        t = _text(li)
        if t and len(bullets) < 6:
            bullets.append(t[:220])
    specs: list[tuple[str, str]] = []
    for tr in doc.xpath('//*[@id="productDetails_techSpec_section_1"]//tr'
                        ' | //*[@id="productDetails_detailBullets_sections1"]//tr'):
        th = _first(tr, "./th")
        td = _first(tr, "./td")
        if th is not None and td is not None:
            k, v = _text(th), _text(td)
            if k and v and len(specs) < 10:
                specs.append((k[:40], v[:80]))
    if not specs:
        for li in doc.xpath('//*[@id="detailBullets_feature_div"]//li'):
            bold = _first(li, './/span[contains(@class,"a-text-bold")]')
            if bold is None:
                continue
            k = _text(bold).rstrip(": ‏‎").strip(" :‏‎")
            v = _text(li)[len(_text(bold)):].strip(" :‏‎")
            if k and v and len(specs) < 10:
                specs.append((k[:40], v[:80]))
    variations = []
    for lab in doc.xpath('//*[starts-with(@id,"variation_")]//label[contains(@class,"a-form-label")]'):
        name = _text(lab).rstrip(":")
        sel = _first(lab, './following-sibling::span[contains(@class,"selection")]',
                     '../..//span[contains(@class,"selection")]')
        if name:
            variations.append(f"{name}: {_text(sel)}" if sel is not None and _text(sel) else name)
    can_add = bool(doc.xpath('//*[@id="add-to-cart-button"] | //input[@name="submit.add-to-cart"]'))
    max_q = None
    opts = doc.xpath('//select[@id="quantity"]/option/@value')
    nums = [int(o) for o in opts if str(o).isdigit()]
    if nums:
        max_q = max(nums)
    return Listing(asin=asin, title=title[:200], price=price, availability=availability,
                   rating=rating, reviews=reviews, prime=prime, brand=brand, image=image,
                   bullets=tuple(bullets), specs=tuple(specs),
                   variations="; ".join(variations)[:160], can_add=can_add, max_quantity=max_q)


# ----- model-facing text -----
def _money_text(p: float | None) -> str:
    return f"${p:,.2f}" if p is not None else "price not shown"


def _rating_text(rating: float | None, reviews: int | None) -> str:
    if rating is None:
        return "no rating"
    out = f"{rating:.1f} stars"
    if reviews:
        out += f" ({reviews:,} ratings)"
    return out


def format_products(query: str, rows: list[Product]) -> str:
    """The search page as a numbered list the model reads and picks from. Each line carries what
    a shopper's eye reads: price, stars, Prime, and the ASIN to stage."""
    if not rows:
        return (f"Amazon search for '{query}' returned no product cards. Try different words "
                "(a part number, the maker's name, or a plainer description).")
    lines = [f"Amazon results for '{query}' (live, top {len(rows)}):"]
    for n, r in enumerate(rows, start=1):
        bits = [_money_text(r.price) + (f" ({r.unit_price})" if r.unit_price else ""),
                _rating_text(r.rating, r.reviews)]
        if r.prime:
            bits.append("Prime")
        if r.bought:
            bits.append(f"{r.bought} bought last month")
        if r.sponsored:
            bits.append("sponsored")
        lines.append(f"{n}. {r.title} — {'; '.join(bits)} — ASIN {r.asin}")
    lines.append("Pick by ASIN — staging verifies the listing and reads its live price. "
                 "Product cards with pictures are showing on the user's screen now.")
    return "\n".join(lines)


def format_listing(l: Listing) -> str:
    """A product page as a compact read-out: enough to answer 'is this the right part?'."""
    lines = [f"{l.title} (ASIN {l.asin})"]
    facts = [_money_text(l.price), _rating_text(l.rating, l.reviews)]
    if l.prime:
        facts.append("Prime")
    if l.brand:
        facts.append(f"by {l.brand}")
    if l.availability:
        facts.append(l.availability)
    lines.append("; ".join(facts))
    if l.variations:
        lines.append(f"Options on the page: {l.variations} — the ASIN is for the selected option.")
    if l.specs:
        lines.append("Details: " + "; ".join(f"{k}: {v}" for k, v in l.specs))
    if l.bullets:
        lines.append("About: " + " | ".join(l.bullets[:4]))
    if not l.can_add:
        lines.append("This page has no plain Add-to-Cart button (an option must be picked, or it "
                     "sells through 'See all buying options'); HELIX may need the user to click.")
    return "\n".join(lines)
