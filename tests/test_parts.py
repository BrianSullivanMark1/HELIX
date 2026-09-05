"""Parts lists — the durable BOM behind a project, and the ledger of what was handed to Amazon."""
from __future__ import annotations

from helix.services.parts import PartsService


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def _svc():
    ticks = iter(range(1, 100))
    return PartsService(_Store(), clock=lambda: f"2026-09-04T12:{next(ticks):02d}")


def test_save_creates_rows_and_upserts_by_name():
    s = _svc()
    out = s.save("IronEye", [
        {"name": "INMP441 mic", "quantity": 1, "spec": "I2S MEMS", "status": "need"},
        {"name": "MAX98357A amp", "quantity": 1, "asin": "https://www.amazon.com/dp/B0G19Z4LLS", "price": 9.99},
        {"name": "ESP32 dev board", "status": "on hand"},
    ])
    assert "Saved to the 'IronEye' parts list" in out and "(B0G19Z4LLS)" in out
    rows = {r.name: r for r in s.rows("IronEye")}
    assert rows["INMP441 mic"].spec == "I2S MEMS" and rows["INMP441 mic"].status == "need"
    assert rows["MAX98357A amp"].asin == "B0G19Z4LLS" and rows["MAX98357A amp"].price == 9.99
    assert rows["ESP32 dev board"].status == "on_hand"
    # An update by name keeps what it doesn't mention.
    s.save("iron eye", [{"name": "inmp441 MIC", "quantity": 3}])
    r = {r.name: r for r in s.rows("IronEye")}["INMP441 mic"]
    assert r.quantity == 3 and r.spec == "I2S MEMS"
    assert s.projects() == ["IronEye"]  # the spoken variant matched the saved name


def test_save_refuses_a_fake_asin_but_keeps_the_row():
    s = _svc()
    out = s.save("P", [{"name": "widget", "asin": "not-an-asin"}, "garbage", {"quantity": 2}])
    assert "Skipped: widget (not a real ASIN" in out and "unnamed" in out
    assert s.rows("P")[0].asin == ""
    assert "Which project" in s.save("", [{"name": "x"}])
    assert "Nothing to save" in s.save("P", [])


def test_remove_by_name_asin_or_everything():
    s = _svc()
    s.save("P", [{"name": "screws", "asin": "B08N5WRWNW"}, {"name": "nuts"}])
    assert "Took screws off 'P'" in s.remove("P", "b08n5wrwnw")
    assert "Nothing in 'P' matches" in s.remove("P", "bolts")
    assert "don't have a parts list" in s.remove("Q", "x")
    assert "Dropped the whole 'P'" in s.remove("P", "everything")
    assert s.projects() == []


def test_resolve_status_and_the_ledger():
    s = _svc()
    s.save("IronEye", [{"name": "28mm speaker", "quantity": 2}, {"name": "LiPo", "quantity": 1}])
    assert s.resolve("IronEye", "28mm speaker", "b0c49rz9wj", 6.99)
    assert not s.resolve("IronEye", "nothing like it", "B0C49RZ9WJ", None)
    assert not s.resolve("NoSuch", "LiPo", "B0C49RZ9WJ", None)
    assert s.set_status("IronEye", ["B0C49RZ9WJ"], "staged") == ["28mm speaker"]
    assert s.set_status("IronEye", ["B0C49RZ9WJ"], "bogus") == []
    s.record_handoff(project="IronEye", how="chrome", est_total=13.98,
                     items=[{"label": "28mm speaker", "asin": "B0C49RZ9WJ", "quantity": 2, "price": 6.99}])
    s.set_status("IronEye", ["B0C49RZ9WJ"], "carted")
    text = s.show("IronEye")
    assert "28mm speaker — qty 2; carted; ASIN B0C49RZ9WJ; about $6.99" in text
    assert "1 of 2 still needed; 1 without an Amazon id yet (LiPo)" in text
    assert "Handed to Amazon" in text and "about $13.98" in text and "(chrome)" in text
    assert s.ledger("iron")[0]["est_total"] == 13.98 and s.ledger("other") == []


def test_show_describes_and_never_coaches_a_fenced_tool():
    s = _svc()
    assert "No parts lists are saved yet" in s.show()
    s.save("A", [{"name": "x", "asin": "B08N5WRWNW", "price": 2}])
    s.save("B", [{"name": "y"}])
    everything = s.show()
    assert "Parts list 'A'" in everything and "Parts list 'B'" in everything
    assert "No parts list called 'C'" in s.show("C") and "Saved lists: A, B" in s.show("C")
    for text in (everything, s.show("A")):
        for fenced in ("stage_parts", "add_to_cart", "open_cart", "save_parts", "check_amazon_cart"):
            assert fenced not in text


def test_the_store_round_trips_through_plain_json_shapes():
    store = _Store()
    a = PartsService(store, clock=lambda: "t1")
    a.save("P", [{"name": "screws", "quantity": 4, "asin": "B08N5WRWNW", "price": 3.5, "note": "M3"}])
    b = PartsService(store, clock=lambda: "t2")  # a fresh service over the same file
    r = b.rows("P")[0]
    assert (r.name, r.quantity, r.asin, r.price, r.note, r.updated) == ("screws", 4, "B08N5WRWNW", 3.5, "M3", "t1")
