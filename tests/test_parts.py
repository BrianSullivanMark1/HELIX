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


# ----- physical fields (MAKER_FLOW §3) -----
def test_physical_fields_round_trip_and_resolve_the_library_key():
    s = _svc()
    out = s.save("IronEye", [
        {"name": "Camera brain", "component": "xiao s3 sense", "face": "front"},
        {"name": "LiPo", "spec": "603048", "on_lid": True, "length": "48 mm", "width": 30, "height": 6.0},
        {"name": "Mystery amp", "component": "flux capacitor", "face": "sideways", "length": -3},
        {"name": "Plain"},
    ])
    assert "[xiao_esp32s3_sense]" in out
    assert "'flux capacitor' isn't a library part" in out and "face 'sideways' isn't one of" in out
    rows = {r.name: r for r in s.rows("IronEye")}
    cam, lipo, amp, plain = rows["Camera brain"], rows["LiPo"], rows["Mystery amp"], rows["Plain"]
    assert cam.component == "xiao_esp32s3_sense" and cam.face == "front" and not cam.on_lid and cam.dims is None
    assert lipo.on_lid and lipo.dims == (48.0, 30.0, 6.0)
    assert amp.component == "flux capacitor" and amp.face == "" and amp.length is None
    assert plain.component == "" and plain.face == "" and plain.on_lid is False and plain.dims is None
    # a fresh service over the same store reads them back; older rows without the fields still load
    store = s._store
    store.d["projects"]["IronEye"].append({"name": "Old row", "quantity": 2})
    fresh = PartsService(store, clock=lambda: "t2")
    again = {r.name: r for r in fresh.rows("IronEye")}
    assert again["Camera brain"].component == "xiao_esp32s3_sense" and again["LiPo"].dims == (48.0, 30.0, 6.0)
    assert again["Old row"].quantity == 2 and again["Old row"].dims is None and again["Old row"].component == ""
    # an update that omits the physical fields keeps them; an explicit empty clears them
    s.save("iron eye", [{"name": "camera brain", "quantity": 2}])
    assert s.rows("IronEye")[0].component == "xiao_esp32s3_sense" and s.rows("IronEye")[0].face == "front"
    s.save("iron eye", [{"name": "camera brain", "component": "", "face": "", "on_lid": False}])
    assert s.rows("IronEye")[0].component == "" and s.rows("IronEye")[0].face == ""


def test_show_prints_the_physical_fields_when_set():
    s = _svc()
    s.save("P", [{"name": "cam", "component": "xiao s3 sense", "face": "front", "length": 21, "width": 17.8, "height": 15},
                 {"name": "cell", "on_lid": True}, {"name": "odd", "component": "unobtainium"}, {"name": "plain"}])
    text = s.show("P")
    assert "cam — qty 1; still needed; library part xiao_esp32s3_sense; 21 × 17.8 × 15 mm; reaches the front wall" in text
    assert "cell — qty 1; still needed; on the lid" in text
    assert "odd — qty 1; still needed; part 'unobtainium' (not in the library)" in text
    assert "4. plain — qty 1; still needed\n" in text
    for fenced in ("stage_parts", "add_to_cart", "open_cart", "save_parts", "design_enclosure", "camera_measure"):
        assert fenced not in text


def test_set_dims_records_measured_millimetres():
    s = _svc()
    s.save("IronEye", [{"name": "XIAO board", "note": "the camera one"}, {"name": "Speaker"}])
    assert s.set_dims("iron eye", "xiao", 4, "21.1 mm", 17.6)          # sorted L >= W >= H, text tolerated
    r = s.rows("IronEye")[0]
    assert r.dims == (21.1, 17.6, 4.0) and r.note == "the camera one dims measured" and r.updated == "2026-09-04T12:03"
    assert s.set_dims("IronEye", "speaker", 28, 28, 5, source="listing")
    assert s.rows("IronEye")[1].note == "dims listing"
    assert s.set_dims("IronEye", "xiao", 21.1, 17.6, 4.2)               # again: the note doesn't repeat
    assert s.rows("IronEye")[0].note == "the camera one dims measured" and s.rows("IronEye")[0].height == 4.2
    assert not s.set_dims("IronEye", "nothing like it", 1, 2, 3)
    assert not s.set_dims("NoSuch", "xiao", 1, 2, 3)
    assert not s.set_dims("IronEye", "xiao", 0, 2, 3) and not s.set_dims("IronEye", "xiao", "wide", 2, 3)
