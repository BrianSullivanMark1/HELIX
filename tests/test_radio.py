"""HELIX RADIO: the catalog is the only state; uploads go to tracks/ or videos/; hide never deletes;
station names resolve local -> catalog -> default; the bucket adapter speaks in sentences."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from helix.adapters import gcs_radio
from helix.adapters.gcloud_fleet import Ran
from helix.ports.radio import NO_BUCKET, NO_CATALOG, Outcome
from helix.services.radio import RadioService, kind_of, NOT_WEB, UNKNOWN_EXT, empty_catalog


class _Store:
    """A bucket in memory."""

    def __init__(self, catalog=None, bucket="helix-radio-test"):
        self.objects = {}
        self._bucket = bucket
        if catalog is not None:
            self.objects["catalog.json"] = json.dumps(catalog).encode()

    def bucket(self):
        return self._bucket

    def available(self):
        return True, None

    def read_catalog(self):
        raw = self.objects.get("catalog.json")
        if raw is None:
            return None, Outcome(False, NO_CATALOG)
        return json.loads(raw), Outcome(True)

    def write_catalog(self, doc):
        self.objects["catalog.json"] = json.dumps(doc).encode()
        return Outcome(True)

    def upload(self, local, key):
        self.objects[key] = Path(local).read_bytes()
        return Outcome(True)

    def fetch(self, key):
        if key not in self.objects:
            return None, Outcome(False, "gone")
        p = Path("/tmp") / ("helix_radio_test_" + key.replace("/", "__"))
        p.write_bytes(self.objects[key])
        return p, Outcome(True)


def test_kinds_and_the_two_refusals():
    assert kind_of("song.MP3") == ("audio", "audio/mpeg")
    assert kind_of("clip.mp4") == ("video", "video/mp4")
    assert kind_of("clip.mov") == (None, NOT_WEB)
    assert kind_of("notes.txt") == (None, UNKNOWN_EXT)


def test_an_empty_bucket_is_a_radio_with_no_songs(tmp_path):
    r = RadioService(_Store())
    b = r.board()
    assert b["ok"] and b["problem"] is None and b["tracks"] == [] and b["station"] == "HELIX RADIO"


def test_upload_writes_the_file_and_the_catalog(tmp_path):
    store = _Store()
    r = RadioService(store, clock=lambda: __import__("datetime").datetime(2026, 9, 21, tzinfo=__import__("datetime").timezone.utc))
    f = tmp_path / "Overnight.mp3"; f.write_bytes(b"ID3song")
    t, why = r.add_track(f, title="Overnight", artist="Mark 1", theme="chill", bpm=96)
    assert why is None and t["kind"] == "audio" and t["audio"] == f"tracks/{t['id']}.mp3" and t["video"] is None
    assert t["theme"] == "chill" and t["bpm"] == 96 and t["at"] == "2026-09-21T00:00:00Z"
    cat = json.loads(store.objects["catalog.json"])
    assert cat["tracks"][0]["id"] == t["id"] and store.objects[t["audio"]] == b"ID3song"
    v = tmp_path / "video.mp4"; v.write_bytes(b"mp4")
    t2, why = r.add_track(v, title="", theme="nope", bpm=999)
    assert why is None and t2["kind"] == "video" and t2["video"].startswith("videos/") and t2["title"] == "video"
    assert t2["theme"] == "" and t2["bpm"] is None
    assert r.board()["count"] == 2
    bad = tmp_path / "x.mov"; bad.write_bytes(b"x")
    assert r.add_track(bad, title="x") == (None, NOT_WEB)


def test_hide_keeps_the_file_and_drops_it_from_the_deck(tmp_path):
    store = _Store()
    r = RadioService(store)
    f = tmp_path / "a.mp3"; f.write_bytes(b"a")
    t, _ = r.add_track(f, title="a")
    assert r.hide(t["id"]) is None
    assert r.board()["tracks"] == [] and t["audio"] in store.objects        # hidden, never deleted
    assert "No track" in r.hide("zzz")


def test_station_resolves_local_then_catalog_then_default():
    store = _Store(empty_catalog())
    local = {"v": ""}
    r = RadioService(store, station_override=lambda: local["v"])
    assert r.set_station("MES", "MES FM") is None
    assert r.board("MES")["station"] == "MES FM" and r.board("WMS")["station"] == "HELIX RADIO"
    local["v"] = "Dock Radio"
    assert r.board("MES")["station"] == "Dock Radio"


def test_play_fetches_once_from_the_bucket(tmp_path):
    store = _Store()
    r = RadioService(store)
    f = tmp_path / "a.mp3"; f.write_bytes(b"bytes")
    t, _ = r.add_track(f, title="a")
    local, mime, why = r.play_file(t["id"])
    assert why is None and mime == "audio/mpeg" and local.read_bytes() == b"bytes"
    assert r.play_file("nope")[2] == "No track with that id."


# ------------------------------------------------------------------------------ the adapter

def _replay(script):
    def runner(argv, timeout):
        key = " ".join(argv[1:4])
        for k, ran in script.items():
            if key.startswith(k):
                return ran
        return Ran(1, "", "unexpected: " + " ".join(argv))
    return runner


def test_gcloud_adapter_reads_writes_and_says_when_there_is_no_catalog(tmp_path):
    cat = {"version": 1, "stations": {"default": "HELIX RADIO"}, "tracks": []}
    g = gcs_radio.GcsRadio(lambda: "gs://helix-radio-mark1/", runner=_replay({
        "storage cat gs://helix-radio-mark1/catalog.json": Ran(0, json.dumps(cat), ""),
        "storage cp": Ran(0, "", ""),
        "storage ls": Ran(0, "gs://helix-radio-mark1/catalog.json\n", ""),
    }), cache_dir=tmp_path)
    assert g.bucket() == "helix-radio-mark1" and g.available() == (True, None)
    doc, out = g.read_catalog()
    assert out.ok and doc == cat
    assert g.write_catalog(cat).ok and (tmp_path / "catalog.upload.json").exists()
    missing = gcs_radio.GcsRadio(lambda: "b", runner=_replay({"storage cat": Ran(1, "", "ERROR: (gcloud.storage.cat) The following URLs matched no objects or files: gs://b/catalog.json")}), cache_dir=tmp_path)
    assert missing.read_catalog() == (None, Outcome(False, NO_CATALOG))
    none = gcs_radio.GcsRadio(lambda: "", runner=_replay({}), cache_dir=tmp_path)
    assert none.available() == (False, NO_BUCKET)


def test_fetch_caches_by_key(tmp_path):
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        if argv[1:3] == ["storage", "cp"]:
            Path(argv[-1]).write_bytes(b"mp3")
        return Ran(0, "", "")

    g = gcs_radio.GcsRadio(lambda: "b", runner=runner, cache_dir=tmp_path)
    p1, out = g.fetch("tracks/abc.mp3")
    p2, _ = g.fetch("tracks/abc.mp3")
    assert out.ok and p1 == p2 and p1.name == "tracks__abc.mp3" and len(calls) == 1


# ------------------------------------------------------------------------------ the routes

def test_routes_shape_and_the_503(tmp_path):
    from fastapi import FastAPI
    from helix.api import radio_routes
    from tests.test_face_build import _call
    app = FastAPI()
    radio_routes.mount_radio(app, SimpleNamespace(radio=RadioService(_Store(empty_catalog())), settings=None))
    st, doc = _call(app, "GET", "/api/radio")
    assert st == 200 and doc["station"] == "HELIX RADIO" and doc["bucket"] == "helix-radio-test"
    bare = FastAPI()
    radio_routes.mount_radio(bare, SimpleNamespace())
    assert _call(bare, "GET", "/api/radio")[0] == 503
    paths = {r.path for r in app.routes if r.path.startswith("/api/radio")}
    assert paths == {"/api/radio", "/api/radio/upload", "/api/radio/play/{tid}", "/api/radio/station", "/api/radio/hide", "/api/radio/bucket"}
    assert not any("delete" in p for p in paths)
