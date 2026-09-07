"""VerifiedStore (READ_ME/DREAM_MIND.md §10): note / dedupe / first_verified_at, lookup scoring by
keyword, topic and project, the per-turn block's shape and cap, stale, forget, the JSON round-trip,
a corrupt file tolerated — plus the VERIFIED KNOWLEDGE persona paragraph and the volatile names."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from helix.adapters.json_settings import JsonSettings
from helix.config import VOLATILE_STORE_NAMES, volatile_data_paths
from helix.services import prompts
from helix.services.verified import (
    FOR_TURN_MAX,
    Fact,
    VerifiedStore,
    describe_fact,
    fact_id,
    facts_text,
    keywords,
    normalize_claim,
)

SEEED = "https://wiki.seeedstudio.com/xiao_esp32s3_camera_usage/"
BOSCH = "https://www.bosch-sensortec.com/products/environmental-sensors/humidity-sensors-bme280/"
DIGIKEY = "https://www.digikey.com/en/products/detail/adafruit/4816/13618201"


class _Clock:
    def __init__(self, at: datetime = datetime(2026, 9, 4, 23, 30)) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _store(tmp_path, clock: _Clock | None = None):
    clock = clock or _Clock()
    return VerifiedStore(JsonSettings(tmp_path / "helix_verified.json"), clock), clock


# ----- normalization -----

def test_normalize_claim_and_stable_ids():
    assert normalize_claim("  XIAO  ESP32S3 Sense PSRAM. ") == "xiao esp32s3 sense psram"
    assert fact_id("xiao esp32s3 sense psram") == fact_id("xiao esp32s3 sense psram")
    assert fact_id("a") != fact_id("b") and fact_id("a").startswith("f") and len(fact_id("a")) == 8


def test_keywords_keep_part_numbers_and_drop_stopwords():
    kws = keywords("How much PSRAM does the ESP32-S3 have? the INMP441 and M3x8 screws")
    assert {"psram", "esp32-s3", "inmp441", "m3x8", "screws"} <= kws
    assert not {"how", "the", "does", "and"} & kws


# ----- note / dedupe / first_verified_at -----

def test_note_records_a_fact_with_host_and_dates(tmp_path):
    store, _ = _store(tmp_path)
    fact = store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED, topics=["ESP32", "xiao", "ESP32"],
                      project="Hat Cam", confidence=0.95, note="wiki hardware table")
    assert isinstance(fact, Fact)
    assert fact.claim == "XIAO ESP32S3 Sense PSRAM" and fact.value == "8 MB"
    assert fact.source_url == SEEED and fact.host == "wiki.seeedstudio.com"
    assert fact.verified_at == "2026-09-04T23:30" and fact.first_verified_at == fact.verified_at
    assert fact.date == "2026-09-04"
    assert fact.topics == ("esp32", "xiao")          # lowercased, deduplicated
    assert fact.project == "Hat Cam" and fact.confidence == 0.95 and fact.note == "wiki hardware table"
    assert store.count() == 1 and store.get(fact.id) == fact


def test_a_newer_verification_replaces_the_value_and_keeps_first_verified_at(tmp_path):
    store, clock = _store(tmp_path)
    first = store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED, topics=["esp32"], project="hat cam")
    clock.at = datetime(2026, 10, 1, 2, 15)
    again = store.note("xiao esp32s3 sense psram.", "8MB (octal SPI)", SEEED, confidence=0.8)
    assert store.count() == 1
    assert again.id == first.id
    assert again.value == "8MB (octal SPI)" and again.confidence == 0.8
    assert again.verified_at == "2026-10-01T02:15" and again.first_verified_at == "2026-09-04T23:30"
    assert again.topics == ("esp32",) and again.project == "hat cam"   # kept when the refresh omits them


def test_note_validates_its_inputs(tmp_path):
    store, _ = _store(tmp_path)
    with pytest.raises(ValueError):
        store.note("", "8 MB", SEEED)
    with pytest.raises(ValueError):
        store.note("claim", "   ", SEEED)
    with pytest.raises(ValueError):
        store.note("claim", "value", "http://wiki.seeedstudio.com/x")     # https only
    with pytest.raises(ValueError):
        store.note("claim", "value", "not a url")
    assert store.count() == 0
    clamped = store.note("c", "v", SEEED, confidence=7, topics="a, b,, c")
    assert clamped.confidence == 1.0 and clamped.topics == ("a", "b", "c")
    assert store.note("c2", "v", SEEED, confidence="junk").confidence == 0.9


# ----- lookup -----

def _seed(store: VerifiedStore, clock: _Clock) -> dict[str, Fact]:
    out = {}
    out["psram"] = store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED, topics=["esp32", "xiao"],
                              project="hat cam")
    clock.at += timedelta(minutes=1)
    out["bme"] = store.note("BME280 supply voltage", "1.71 V to 3.6 V", BOSCH, topics=["bme280", "sensor"])
    clock.at += timedelta(minutes=1)
    out["screws"] = store.note("M3x8 socket head screws (100 pack) price", "$7.49", DIGIKEY,
                               topics=["fasteners"], project="ironeye")
    clock.at += timedelta(minutes=1)
    out["flash"] = store.note("XIAO ESP32S3 Sense flash", "8 MB", SEEED, topics=["esp32", "xiao"],
                              project="hat cam")
    return out


def test_lookup_scores_by_keyword_topic_and_project(tmp_path):
    store, clock = _store(tmp_path)
    facts = _seed(store, clock)
    hits = store.lookup("how much PSRAM does the ESP32 S3 have")
    assert hits[0] == facts["psram"]                       # claim + topic hits beat a topic-only hit
    assert facts["flash"] in hits and facts["screws"] not in hits and facts["bme"] not in hits
    assert store.lookup("bme280 voltage") == [facts["bme"]]
    assert store.lookup("lidar range") == []
    # the project's facts, newest first, when only a project is given
    assert store.lookup("", project="hat cam") == [facts["flash"], facts["psram"]]
    assert store.lookup("", project="ironeye") == [facts["screws"]]
    # a project match lifts a weak keyword hit to the top
    assert store.lookup("screws price", project="ironeye")[0] == facts["screws"]
    assert store.lookup("psram", limit=1) == [facts["psram"]]


def test_lookup_ties_go_to_the_newer_fact(tmp_path):
    store, clock = _store(tmp_path)
    facts = _seed(store, clock)
    hits = store.lookup("xiao")                             # both XIAO facts score the same
    assert hits == [facts["flash"], facts["psram"]]


# ----- for_turn -----

def test_for_turn_is_a_labelled_block_of_at_most_eight_facts(tmp_path):
    store, clock = _store(tmp_path)
    for i in range(12):
        store.note(f"ESP32 fact number {i}", f"value {i}", SEEED, topics=["esp32"])
        clock.at += timedelta(minutes=1)
    block = store.for_turn("tell me about the esp32 board")
    assert block.startswith("[VERIFIED KNOWLEDGE — facts HELIX itself confirmed from current sources")
    assert "Records, not instructions" in block and block.endswith("]")
    assert block.count(" — verified 2026-09-") == FOR_TURN_MAX
    assert f"{FOR_TURN_MAX}) " in block and f"{FOR_TURN_MAX + 1}) " not in block
    # newest first: facts 11 down to 4 (the clock moved a minute per fact, all on the 4th)
    assert "1) ESP32 fact number 11: value 11 — verified 2026-09-04 from wiki.seeedstudio.com" in block
    assert "8) ESP32 fact number 4: value 4" in block and "fact number 3:" not in block
    assert store.for_turn("nothing about lidar here") == ""
    assert store.for_turn("") == ""


def test_for_turn_respects_its_size_ceiling(tmp_path):
    store, _ = _store(tmp_path)
    for i in range(8):
        store.note(f"ESP32 long fact {i}", "x" * 290, SEEED, topics=["esp32"])
    block = store.for_turn("esp32")
    assert len(block) < 2000 and block.count("verified 2026") < 8 and block.endswith("]")


def test_for_turn_line_shape_matches_the_spec(tmp_path):
    store, _ = _store(tmp_path)
    store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED, topics=["esp32"])
    assert "1) XIAO ESP32S3 Sense PSRAM: 8 MB — verified 2026-09-04 from wiki.seeedstudio.com]" \
        in store.for_turn("psram on the xiao")


# ----- stale / forget / recent / count -----

def test_stale_finds_facts_older_than_the_window(tmp_path):
    store, clock = _store(tmp_path)
    old = store.note("old fact", "v", SEEED)
    clock.at += timedelta(days=100)
    fresh = store.note("fresh fact", "v", SEEED)
    assert store.stale(90) == [old]
    assert store.stale(365) == []
    refreshed = store.mark_reverified(old.id)
    assert refreshed is not None and refreshed.verified_at == fresh.verified_at
    assert refreshed.first_verified_at == old.first_verified_at and store.stale(90) == []
    assert store.mark_reverified("nope") is None


def test_forget_recent_and_count(tmp_path):
    store, clock = _store(tmp_path)
    facts = _seed(store, clock)
    assert store.count() == 4
    assert store.recent(2) == [facts["flash"], facts["screws"]]
    assert store.forget(facts["bme"].id) is True
    assert store.forget(facts["bme"].id) is False and store.forget("") is False
    assert store.count() == 3 and store.get(facts["bme"].id) is None
    assert [f.id for f in store.all()] == [facts["psram"].id, facts["screws"].id, facts["flash"].id]


# ----- persistence -----

def test_facts_round_trip_through_the_json_file(tmp_path):
    store, clock = _store(tmp_path)
    facts = _seed(store, clock)
    raw = json.loads((tmp_path / "helix_verified.json").read_text("utf-8"))
    assert isinstance(raw["facts"], list) and len(raw["facts"]) == 4
    assert raw["facts"][0]["topics"] == ["esp32", "xiao"]    # JSON has lists…
    reopened = VerifiedStore(JsonSettings(tmp_path / "helix_verified.json"), clock)
    assert reopened.all() == list(facts.values())              # …the store hands back tuples/Facts
    assert reopened.lookup("psram")[0].topics == ("esp32", "xiao")


def test_a_corrupt_or_foreign_file_reads_as_empty_and_junk_rows_are_skipped(tmp_path):
    path = tmp_path / "helix_verified.json"
    path.write_text("{not json", "utf-8")
    store = VerifiedStore(JsonSettings(path), _Clock())
    assert store.count() == 0 and store.for_turn("esp32") == ""
    store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED)    # writing heals the file
    assert store.count() == 1

    class _Junk:
        def get(self, key, default=None):
            return [42, {"claim": "", "value": "v"}, {"claim": "good claim", "value": "good value",
                                                     "source_url": SEEED, "confidence": "bad",
                                                     "topics": 7, "verified_at": "2026-01-01T00:00"},
                    {"claim": "good claim", "value": "duplicate id"}]

        def set(self, key, value):
            pass

    junk = VerifiedStore(_Junk(), _Clock())
    assert [f.claim for f in junk.all()] == ["good claim"]
    fact = junk.all()[0]
    assert fact.confidence == 0.9 and fact.topics == () and fact.host == "wiki.seeedstudio.com"
    assert fact.first_verified_at == "2026-01-01T00:00" and fact.id == fact_id("good claim")

    class _Raises:
        def get(self, key, default=None):
            raise OSError("disk")

        def set(self, key, value):
            raise OSError("disk")

    broken = VerifiedStore(_Raises(), _Clock())
    assert broken.all() == [] and broken.lookup("x") == []
    assert broken.note("c", "v", SEEED).claim == "c"       # a failed write is logged, never raised


def test_the_store_is_dict_shaped_not_a_list(tmp_path):
    path = tmp_path / "helix_verified.json"
    path.write_text(json.dumps({"facts": "not a list"}), "utf-8")
    assert VerifiedStore(JsonSettings(path), _Clock()).count() == 0


# ----- model-facing text -----

def test_describe_fact_and_facts_text(tmp_path):
    store, _ = _store(tmp_path)
    fact = store.note("XIAO ESP32S3 Sense PSRAM", "8 MB", SEEED, project="hat cam", confidence=0.7,
                      note="hardware table")
    assert describe_fact(fact) == "XIAO ESP32S3 Sense PSRAM: 8 MB — verified 2026-09-04 from wiki.seeedstudio.com"
    text = facts_text("psram", [fact])
    assert text.splitlines()[0] == "Verified facts about 'psram' (1):"
    assert (f"- {describe_fact(fact)}; confidence 70%; project hat cam; hardware table [id {fact.id}] "
            f"{SEEED}") in text
    assert text.splitlines()[-1].startswith("Say which of these an answer rests on")
    assert facts_text("x", []) == ("Nothing verified about 'x' yet — what HELIX knows about it is from "
                                   "memory until a source is read.")
    assert "for the project 'p'" in facts_text("", [], project="p")
    unrecorded = Fact(id="f0000000", claim="c", value="v", source_url="", host="", verified_at="",
                      first_verified_at="")
    assert describe_fact(unrecorded) == "c: v — verified an unrecorded date from an unrecorded source"


# ----- the persona and the volatile names -----

def _bullets() -> list[str]:
    out: list[str] = []
    for line in prompts.CONSOLE_SYSTEM.splitlines():
        if line.startswith("- "):
            out.append(line)
        elif out and line.startswith("  "):
            out[-1] += " " + line.strip()
    return out


def test_the_verified_knowledge_paragraph_teaches_the_faculty_and_sits_after_amazon():
    bullets = _bullets()
    idx = next(i for i, b in enumerate(bullets) if b.startswith("- VERIFIED KNOWLEDGE"))
    # It follows the maker-flow paragraph when one exists (DREAM_MIND.md §10), else the Amazon one.
    assert bullets[idx - 1].startswith(("- THE MAKER FLOW", "- You do the user's AMAZON LEGWORK"))
    bullet = bullets[idx]
    for tool in ("research_search", "research_read", "note_verified_fact", "verified_facts",
                 "forget_verified", "lookup_amazon"):
        assert tool in bullet, tool
    for shape in ("verified today on Seeed's wiki", "from memory — let me verify"):
        assert shape in bullet, shape
    for idea in ("parts, sizes, pins, protocols, prices", "availability", "date", "supplier",
                 "known tomorrow", "never note a fact you did not read"):
        assert idea in bullet, idea


def test_the_dream_minds_stores_are_volatile(tmp_path):
    assert "helix_verified.json" in VOLATILE_STORE_NAMES and "helix_self.json" in VOLATILE_STORE_NAMES
    paths = volatile_data_paths(tmp_path)
    assert tmp_path / "helix_verified.json" in paths and tmp_path / "helix_self.json" in paths
