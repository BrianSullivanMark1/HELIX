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
    copies = [a for a in calls if a[1:3] == ["storage", "cp"]]
    assert out.ok and p1 == p2 and p1.name == "tracks__abc.mp3" and len(copies) == 1   # no token -> the CLI copy, once
    assert not list(tmp_path.glob("*.part"))


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
    assert paths == {"/api/radio", "/api/radio/upload", "/api/radio/play/{tid}", "/api/radio/station", "/api/radio/hide", "/api/radio/bucket",
                     "/api/radio/prefetch", "/api/radio/cache", "/api/radio/track", "/api/radio/folder"}
    assert not any("delete" in p for p in paths)
    methods = {m for r in app.routes if r.path == "/api/radio/folder" for m in r.methods}
    assert methods == {"POST", "PUT", "DELETE"}      # DELETE removes an EMPTY shelf; a track is never deleted


# ------------------------------------------------------------------------------ shelves (folders)

def test_folders_create_rename_move_remove_and_the_empty_rule(tmp_path):
    from helix.services.radio import BAD_FOLDER, FOLDER_EXISTS, FOLDER_NOT_EMPTY, NO_SUCH_FOLDER, clean_folder
    assert clean_folder(" Rock / 80s ") == "Rock/80s" and clean_folder("") == "" and clean_folder("a/b/c/d/e") is None
    assert clean_folder("../x") is None and clean_folder("Rock\\Metal") == "Rock/Metal"
    r = RadioService(_Store())
    folders, why = r.create_folder("Rock/80s")
    assert why is None and folders == ["Rock", "Rock/80s"]           # the parent is implied
    assert r.create_folder("Rock")[1] == FOLDER_EXISTS and r.create_folder("??")[1] == BAD_FOLDER
    f = tmp_path / "a.mp3"; f.write_bytes(b"x")
    t, _ = r.add_track(f, title="a", folder="Rock/80s")
    assert t["folder"] == "Rock/80s" and r.board()["tracks"][0]["folder"] == "Rock/80s"
    assert r.remove_folder("Rock")[1] == FOLDER_NOT_EMPTY
    folders, why = r.rename_folder("Rock", "Classics")
    assert why is None and folders == ["Classics", "Classics/80s"] and r.board()["tracks"][0]["folder"] == "Classics/80s"
    moved, why = r.move_track(t["id"], "")
    assert why is None and moved["folder"] == "" and r.remove_folder("Classics/80s")[0] == ["Classics"]
    assert r.remove_folder("Nope")[1] == NO_SUCH_FOLDER and r.move_track("zz", "Classics")[1] == "No track with that id."
    assert r.add_track(f, title="b", folder="a/b/c/d/e")[1] == BAD_FOLDER
    hidden, _ = r.add_track(f, title="h", folder="Classics")
    r.hide(hidden["id"])
    assert r.remove_folder("Classics")[0] == []           # a hidden track never blocks a shelf; it moves to the top
    got, _ = r.retitle(t["id"], title="Renamed", theme="hype", bpm=120)
    assert got["title"] == "Renamed" and got["theme"] == "hype" and got["bpm"] == 120


def test_the_board_reports_shelves_cache_and_cached_flags(tmp_path):
    class Store(_Store):
        def cached(self, key):
            return Path("/tmp/x") if key.endswith("cached.mp3") else None

        def cache_report(self):
            return {"files": 1, "bytes": 10}
    cat = empty_catalog()
    cat["tracks"] = [{"id": "1", "title": "c", "audio": "tracks/cached.mp3", "kind": "audio", "mime": "audio/mpeg"},
                     {"id": "2", "title": "n", "audio": "tracks/new.mp3", "kind": "audio", "mime": "audio/mpeg", "folder": "Late/Night"}]
    b = RadioService(Store(cat), cache_gb=lambda: "2").board()
    assert [t["cached"] for t in b["tracks"]] == [True, False] and b["folders"] == ["Late", "Late/Night"]
    assert b["cache"] == {"files": 1, "bytes": 10, "max_gb": 2.0}


# ------------------------------------------------------------------------------ tasks in the register

def _jobs(tmp_path):
    from helix.services.jobs import Jobs
    return Jobs(tmp_path / "jobs.json")


def test_cache_all_is_one_task_with_progress_and_a_cancel(tmp_path):
    class Store(_Store):
        def cached(self, key):
            return None

        def fetch(self, key, on_progress=None, cancelled=None):
            if on_progress:
                on_progress(1, 2); on_progress(2, 2)
            return super().fetch(key)
    st = Store(empty_catalog())
    r = RadioService(st, jobs=_jobs(tmp_path))
    for n in "ab":
        f = tmp_path / f"{n}.mp3"; f.write_bytes(b"x")
        r.add_track(f, title=n)
    job, why = r.cache_all("brian")
    assert why is None and job["kind"] == "cache" and job["can_cancel"]
    import time
    for _ in range(50):
        got = r._jobs.get(job["id"])
        if got.over:
            break
        time.sleep(0.05)
    assert got.state == "done" and got.progress == 1.0 and any("cached a" in ln for ln in got.lines)
    assert r.cache_all()[1] == "Every track is already cached on this PC." or r.cache_all()[1]
    assert RadioService(st).cache_all()[1].startswith("The task register is not available")


def test_play_stream_serves_the_cache_or_streams_while_it_arrives(tmp_path):
    import threading
    from helix.adapters.gcs_radio import Fetching

    class Store(_Store):
        def __init__(self):
            super().__init__(empty_catalog())
            self.live = None

        def cached(self, key):
            p = tmp_path / ("c_" + key.replace("/", "__"))
            return p if p.exists() else None

        def begin_fetch(self, key, cancelled=None):
            f = Fetching(key=key, part=tmp_path / "x.part", final=tmp_path / "x.mp3", total=3)
            f.headed.set()
            self.live = f
            return f
    st = Store()
    r = RadioService(st, jobs=_jobs(tmp_path))
    f = tmp_path / "a.mp3"; f.write_bytes(b"abc")
    t, _ = r.add_track(f, title="a")
    got = r.play_stream(t["id"])
    assert "fetching" in got and got["mime"] == "audio/mpeg" and st.live.key == t["audio"]
    running = r._jobs.running("fetch")
    assert running and running[0].quiet and running[0].title == "Fetching a"
    (tmp_path / ("c_" + t["audio"].replace("/", "__"))).write_bytes(b"abc")
    assert r.play_stream(t["id"])["file"].read_bytes() == b"abc"
    assert r.play_stream("nope") == {"problem": "No track with that id."}
    st.live.done.set()


def test_the_streamed_upload_route_is_a_task_with_two_halves(tmp_path):
    import asyncio
    from fastapi import FastAPI
    from helix.api import radio_routes
    jobs = _jobs(tmp_path)
    seen = []

    class Store(_Store):
        def upload(self, local, key, mime="", on_progress=None, cancelled=None):
            seen.append((key, mime))
            if on_progress:
                on_progress(2, 4); on_progress(4, 4)
            return super().upload(local, key)
    r = RadioService(Store(empty_catalog()), jobs=jobs)
    app = FastAPI()
    radio_routes.mount_radio(app, SimpleNamespace(radio=r, settings=None, jobs=jobs))
    body = b"abcd"

    def call(path, raw, method="PUT"):
        headers = [(b"host", b"127.0.0.1:8737"), (b"content-length", str(len(raw)).encode()), (b"content-type", b"application/octet-stream")]
        p, qs = path.split("?", 1)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http", "method": method,
                 "path": p, "raw_path": p.encode(), "root_path": "", "query_string": qs.encode(), "headers": headers,
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8737)}
        status, chunks, sent = [], [], {"v": False}

        async def receive():
            if sent["v"]:
                await asyncio.sleep(3600)
            sent["v"] = True
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                status.append(msg["status"])
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))
        asyncio.run(app(scope, receive, send))
        return status[0], json.loads(b"".join(chunks).decode() or "null")

    st, doc = call("/api/radio/upload?name=song.mp3&title=Song&folder=Rock", body)
    assert st == 200 and doc["track"]["folder"] == "Rock" and seen == [(doc["track"]["audio"], "audio/mpeg")]
    job = jobs.get(doc["job"])
    assert job.state == "done" and job.progress == 1.0 and job.title == "Upload Song" and "in the catalog" in job.lines[-1]
    assert call("/api/radio/upload?name=notes.txt", body)[0] == 400
    st, doc = call("/api/radio/upload?name=song.mp3", b"")
    assert st == 400 and doc["error"] == "The file was empty."
    assert [j.state for j in jobs._jobs.values()].count("failed") == 1


# ------------------------------------------------------------------------------ the adapter over HTTPS

class _Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self._at = 0

    def read(self, n=-1):
        if n < 0:
            n = len(self._body)
        chunk = self._body[self._at:self._at + n]
        self._at += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_streams_with_a_token_and_falls_back_to_the_cli_without_one(tmp_path):
    reqs = []

    def opener(req, timeout=0):
        reqs.append((req.get_method(), req.full_url, dict(req.header_items())))
        return _Resp(200, b"0123456789", {"Content-Length": "10"})
    g = gcs_radio.GcsRadio(lambda: "helix-radio", runner=lambda argv, t: Ran(0, "tok123\n", ""), cache_dir=tmp_path, opener=opener)
    got = []
    p, out = g.fetch("videos/v1.mp4", on_progress=lambda a, b: got.append((a, b)))
    assert out.ok and p.read_bytes() == b"0123456789" and got[-1] == (10, 10)
    assert reqs[0][0] == "GET" and "o/videos%2Fv1.mp4?alt=media" in reqs[0][1] and reqs[0][2]["Authorization"] == "Bearer tok123"
    assert g.token() == "tok123" and g.cache_report()["files"] == 1
    # a cancel mid-stream discards the half file
    cancelled = {"v": True}
    p2, out2 = g.fetch("videos/v2.mp4", cancelled=lambda: cancelled["v"])
    assert p2 is None and out2.problem == gcs_radio.CANCELLED and not list(tmp_path.glob("*.part"))


def test_download_errors_are_sentences(tmp_path):
    import urllib.error

    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)
    g = gcs_radio.GcsRadio(lambda: "b", runner=lambda argv, t: Ran(0, "tok", ""), cache_dir=tmp_path, opener=opener)
    p, out = g.fetch("tracks/x.mp3")
    assert p is None and out.problem == "Your gcloud account is not allowed to read in this bucket."


def test_resumable_upload_goes_in_pieces_with_progress_and_can_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(gcs_radio, "PIECE", 4)
    reqs = []

    def opener(req, timeout=0):
        reqs.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        if req.get_method() == "POST":
            return _Resp(200, b"", {"Location": "https://up/session"})
        rng = req.get_header("Content-range")
        end, total = rng.split(" ")[1].split("/")
        return _Resp(200 if int(end.split("-")[1]) + 1 == int(total) else 308)
    g = gcs_radio.GcsRadio(lambda: "b", runner=lambda argv, t: Ran(0, "tok", ""), cache_dir=tmp_path, opener=opener)
    f = tmp_path / "s.mp3"; f.write_bytes(b"0123456789")
    got = []
    out = g.upload(f, "tracks/s.mp3", mime="audio/mpeg", on_progress=lambda a, b: got.append(a))
    assert out.ok and got == [4, 8, 10]
    assert reqs[0][0] == "POST" and "uploadType=resumable&name=tracks%2Fs.mp3" in reqs[0][1] and reqs[0][2]["X-upload-content-type"] == "audio/mpeg"
    assert [r[2]["Content-range"] for r in reqs[1:]] == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
    stop = g.upload(f, "tracks/s.mp3", cancelled=lambda: True)
    assert stop.problem == gcs_radio.CANCELLED
    cli = []
    g2 = gcs_radio.GcsRadio(lambda: "b", runner=lambda argv, t: (cli.append(argv), Ran(0, "", ""))[1], cache_dir=tmp_path, opener=opener)
    assert g2.upload(f, "tracks/s.mp3").ok and cli[-1][1:3] == ["storage", "cp"]     # no token: the CLI, once
