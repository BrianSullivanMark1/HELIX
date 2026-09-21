"""HELIX RADIO - the company's music library, played from HELIX and (later) every app.

The catalog is one JSON in the bucket (docs/HELIX_RADIO.md section 4). This service is its only
writer from HELIX: add a track, hide a track, name a station, shelve tracks in folders. Playback
hands the page a local file (the bucket object, cached once) so seeking and video work without
signed links - and a file still arriving is played while it arrives.

Folders ("shelves"): a track sits in one folder, like Explorer; a folder is a path such as
"Rock/80s", four levels deep at most, kept in the catalog so every HELIX sees the same shelves.
Removing a folder needs it empty - tracks are never dropped by shelving. Playlists (a track in
many) are a later layer on top; nothing here forecloses them.

Formats today: web-native ones are kept as they are; the rest are refused in one sentence until
the API's transcoder lands. Deleting is not here and will not be: hide, yes; delete, the GCS
console, a person.

Every long thing here - an upload's bucket phase, a fetch, caching the whole station - is a task
in the register (services/jobs.py) when one is wired, so the page sees it and can stop it.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from helix.ports.radio import NO_CATALOG, Outcome, RadioStore

AUDIO_EXT = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
             ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac"}
VIDEO_EXT = {".mp4": "video/mp4", ".webm": "video/webm", ".m4v": "video/mp4"}
LATER_EXT = {".mov", ".mkv", ".avi", ".wmv", ".mpg", ".mpeg"}
THEMES = ("chill", "hype", "dark", "happy", "focus", "epic")
MAX_NAME = 80
MAX_DEPTH = 4
DEFAULT_CACHE_GB = 5

NOT_WEB = "That format needs the transcoder (coming with the radio API). For now: mp3, m4a, aac, ogg, wav, flac, mp4, webm."
UNKNOWN_EXT = "HELIX does not know that file type. Music: mp3, m4a, aac, ogg, wav, flac. Video: mp4, webm."
NO_SUCH_TRACK = "No track with that id."
NO_SUCH_FOLDER = "No folder with that name."
FOLDER_EXISTS = "A folder with that name is already there."
FOLDER_NOT_EMPTY = "That folder still holds tracks. Move them out first - shelving never drops a track."
BAD_FOLDER = "A folder name is letters, digits, spaces, dashes and dots; up to four levels deep, like Rock/80s."
CANCEL_UPLOAD = "Stops sending. Nothing reaches the catalog; a half-sent file is abandoned by the bucket."
CANCEL_FETCH = "Stops the download; the half file is discarded. The track stays in the bucket."
CANCEL_CACHE = "Stops caching. What is already cached stays cached."


def empty_catalog() -> dict:
    return {"version": 2, "stations": {"default": "HELIX RADIO"}, "folders": [], "tracks": []}


def kind_of(name: str) -> tuple[str | None, str | None]:
    """('audio'|'video', mime) for a file name, or (None, sentence)."""
    ext = Path(name).suffix.lower()
    if ext in AUDIO_EXT:
        return "audio", AUDIO_EXT[ext]
    if ext in VIDEO_EXT:
        return "video", VIDEO_EXT[ext]
    if ext in LATER_EXT:
        return None, NOT_WEB
    return None, UNKNOWN_EXT


def clean_text(s: str, limit: int = MAX_NAME) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def clean_folder(path: str) -> str | None:
    """'Rock / 80s ' -> 'Rock/80s'; '' -> '' (the top); None when it is not a folder name."""
    parts = [clean_text(p, 40) for p in str(path or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    if len(parts) > MAX_DEPTH:
        return None
    for p in parts:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .\-_'&]*", p):
            return None
    return "/".join(parts)


class RadioService:
    def __init__(self, store: RadioStore, *, station_override=None, clock=None, jobs=None, cache_gb=None) -> None:
        self._store = store
        self._override = station_override or (lambda: None)   # the local station name for this HELIX
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs = jobs                                       # services.jobs.Jobs or None
        self._cache_gb = cache_gb or (lambda: None)
        self._lock = threading.Lock()
        self._catalog: dict | None = None

    # ---------------------------------------------------------------- reads
    def board(self, app: str = "default") -> dict:
        """What the deck shows: the catalog (visible tracks), the folders, the station name for this
        app, the bucket, what is cached, and one sentence when something is off."""
        doc, out = self._store.read_catalog()
        if doc is None and out.problem == NO_CATALOG:
            doc, out = empty_catalog(), Outcome(True)      # an empty bucket is a radio with no songs yet
        with self._lock:
            self._catalog = doc
        tracks = [dict(t) for t in (doc or {}).get("tracks", []) if isinstance(t, dict) and not t.get("hidden")]
        cached = getattr(self._store, "cached", None)
        for t in tracks:
            t.setdefault("folder", "")
            key = t.get("video") or t.get("audio")
            t["cached"] = bool(cached and key and cached(key))
        stations = (doc or {}).get("stations") or {}
        return {
            "bucket": self._store.bucket(),
            "ok": out.ok,
            "problem": out.problem,
            "station": self.station_name(app, stations),
            "stations": stations,
            "folders": self.folders(doc),
            "tracks": tracks,
            "count": len(tracks),
            "cache": self.cache_report(),
        }

    @staticmethod
    def folders(doc: dict | None) -> list[str]:
        """Every folder: the explicit ones plus every parent of a track's folder, sorted."""
        found: set[str] = set()
        seeds = [str(f) for f in (doc or {}).get("folders", []) if f]
        seeds += [str(t.get("folder") or "") for t in (doc or {}).get("tracks", []) if isinstance(t, dict)]
        for f in seeds:
            while f:
                found.add(f)
                f = f.rsplit("/", 1)[0] if "/" in f else ""
        return sorted(found, key=str.lower)

    def station_name(self, app: str, stations: dict | None = None) -> str:
        local = (self._override() or "").strip()
        if local:
            return local
        if stations is None:
            stations = (self._catalog or {}).get("stations") or {}
        return str(stations.get(app) or stations.get("default") or "HELIX RADIO")

    def cache_report(self) -> dict:
        rep = getattr(self._store, "cache_report", None)
        out = rep() if rep else {"files": 0, "bytes": 0}
        out["max_gb"] = self.cache_max_gb()
        return out

    def cache_max_gb(self) -> float:
        try:
            v = float(self._cache_gb() or DEFAULT_CACHE_GB)
        except (TypeError, ValueError):
            v = DEFAULT_CACHE_GB
        return max(0.5, min(200.0, v))

    # ---------------------------------------------------------------- the catalog, read fresh for a write
    def _doc_for_write(self) -> tuple[dict | None, str | None]:
        doc, out = self._store.read_catalog()
        if doc is None:
            if out.problem != NO_CATALOG:
                return None, out.problem
            doc = empty_catalog()
        doc.setdefault("folders", [])
        doc.setdefault("tracks", [])
        return doc, None

    def _commit(self, doc: dict) -> str | None:
        wr = self._store.write_catalog(doc)
        if not wr.ok:
            return wr.problem
        with self._lock:
            self._catalog = doc
        return None

    # ---------------------------------------------------------------- writes
    def add_track(self, local: Path, *, title: str, artist: str = "", theme: str = "", bpm: int | None = None,
                  uploaded_by: str = "", folder: str = "", job=None) -> tuple[dict | None, str | None]:
        kind, mime_or_why = kind_of(local.name)
        if kind is None:
            return None, mime_or_why
        shelf = clean_folder(folder)
        if shelf is None:
            return None, BAD_FOLDER
        doc, why = self._doc_for_write()
        if doc is None:
            return None, why
        tid = uuid.uuid4().hex[:6]
        ext = local.suffix.lower()
        key = f"{'videos' if kind == 'video' else 'tracks'}/{tid}{ext}"
        jobs = self._jobs
        total = local.stat().st_size or 1

        def on_progress(sent: int, tot: int | None) -> None:
            if jobs is not None and job is not None:
                jobs.progress(job, 0.5 + 0.5 * (sent / (tot or total)), f"Storing in the bucket - {sent // (1024 * 1024)} of {(tot or total) // (1024 * 1024)} MB")

        def cancelled() -> bool:
            return bool(jobs is not None and job is not None and jobs.cancelled(job))

        try:
            up = self._store.upload(local, key, mime=mime_or_why, on_progress=on_progress, cancelled=cancelled)
        except TypeError:   # a store without the progress signature (tests' memory bucket)
            up = self._store.upload(local, key)
        if not up.ok:
            return None, up.problem
        # keep the upload as the cached copy: the first play is instant
        try:
            cache_dir = getattr(self._store, "cache_dir", None)
            if cache_dir:
                dest = cache_dir() / key.replace("/", "__")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    import shutil
                    shutil.copyfile(local, dest)
        except OSError:
            pass
        track = {
            "id": tid,
            "title": clean_text(title) or local.stem[:MAX_NAME],
            "artist": clean_text(artist),
            "theme": theme if theme in THEMES else "",
            "bpm": int(bpm) if bpm and 40 <= int(bpm) <= 240 else None,
            "kind": kind,
            "mime": mime_or_why,
            "audio": key if kind == "audio" else None,
            "video": key if kind == "video" else None,
            "folder": shelf,
            "bytes": local.stat().st_size,
            "uploaded_by": clean_text(uploaded_by),
            "at": self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        doc["tracks"].append(track)
        if shelf and shelf not in doc["folders"]:
            doc["folders"].append(shelf)
        why = self._commit(doc)
        if why:
            return None, why
        return track, None

    def hide(self, tid: str) -> str | None:
        doc, why = self._doc_for_write()
        if doc is None:
            return why
        for t in doc["tracks"]:
            if t.get("id") == tid:
                t["hidden"] = True
                return self._commit(doc)
        return NO_SUCH_TRACK

    def set_station(self, app: str, name: str) -> str | None:
        name = clean_text(name, 40)
        doc, why = self._doc_for_write()
        if doc is None:
            return why
        stations = doc.setdefault("stations", {})
        key = (app or "default").strip() or "default"
        if name:
            stations[key] = name
        else:
            stations.pop(key, None) if key != "default" else None
        return self._commit(doc)

    # ---------------------------------------------------------------- folders
    def create_folder(self, path: str) -> tuple[list[str] | None, str | None]:
        shelf = clean_folder(path)
        if not shelf:
            return None, BAD_FOLDER
        doc, why = self._doc_for_write()
        if doc is None:
            return None, why
        if shelf in self.folders(doc):
            return None, FOLDER_EXISTS
        doc["folders"].append(shelf)
        why = self._commit(doc)
        return (None, why) if why else (self.folders(doc), None)

    def rename_folder(self, old: str, new: str) -> tuple[list[str] | None, str | None]:
        src, dst = clean_folder(old), clean_folder(new)
        if not src or not dst:
            return None, BAD_FOLDER
        doc, why = self._doc_for_write()
        if doc is None:
            return None, why
        have = self.folders(doc)
        if src not in have:
            return None, NO_SUCH_FOLDER
        if dst in have and dst != src:
            return None, FOLDER_EXISTS

        def moved(f: str) -> str:
            return dst + f[len(src):] if f == src or f.startswith(src + "/") else f

        doc["folders"] = sorted({moved(f) for f in have}, key=str.lower)
        for t in doc["tracks"]:
            t["folder"] = moved(str(t.get("folder") or ""))
        why = self._commit(doc)
        return (None, why) if why else (self.folders(doc), None)

    def remove_folder(self, path: str) -> tuple[list[str] | None, str | None]:
        shelf = clean_folder(path)
        if not shelf:
            return None, BAD_FOLDER
        doc, why = self._doc_for_write()
        if doc is None:
            return None, why
        if shelf not in self.folders(doc):
            return None, NO_SUCH_FOLDER
        for t in doc["tracks"]:
            f = str(t.get("folder") or "")
            if not t.get("hidden") and (f == shelf or f.startswith(shelf + "/")):
                return None, FOLDER_NOT_EMPTY
        doc["folders"] = [f for f in doc["folders"] if not (f == shelf or f.startswith(shelf + "/"))]
        for t in doc["tracks"]:      # hidden tracks left on the shelf go to the top so the shelf really goes
            f = str(t.get("folder") or "")
            if f == shelf or f.startswith(shelf + "/"):
                t["folder"] = ""
        why = self._commit(doc)
        return (None, why) if why else (self.folders(doc), None)

    def move_track(self, tid: str, folder: str) -> tuple[dict | None, str | None]:
        shelf = clean_folder(folder)
        if shelf is None:
            return None, BAD_FOLDER
        doc, why = self._doc_for_write()
        if doc is None:
            return None, why
        for t in doc["tracks"]:
            if t.get("id") == tid:
                t["folder"] = shelf
                if shelf and shelf not in doc["folders"]:
                    doc["folders"].append(shelf)
                why = self._commit(doc)
                return (None, why) if why else (t, None)
        return None, NO_SUCH_TRACK

    def retitle(self, tid: str, *, title: str | None = None, artist: str | None = None,
                theme: str | None = None, bpm: int | None = None) -> tuple[dict | None, str | None]:
        doc, why = self._doc_for_write()
        if doc is None:
            return None, why
        for t in doc["tracks"]:
            if t.get("id") == tid:
                if title is not None and clean_text(title):
                    t["title"] = clean_text(title)
                if artist is not None:
                    t["artist"] = clean_text(artist)
                if theme is not None:
                    t["theme"] = theme if theme in THEMES else ""
                if bpm is not None:
                    t["bpm"] = int(bpm) if 40 <= int(bpm) <= 240 else None
                why = self._commit(doc)
                return (None, why) if why else (t, None)
        return None, NO_SUCH_TRACK

    # ---------------------------------------------------------------- playback
    def _track(self, tid: str) -> tuple[dict | None, str | None]:
        with self._lock:
            doc = self._catalog
        if doc is None:
            doc, out = self._store.read_catalog()
            if doc is None:
                return None, out.problem
            with self._lock:
                self._catalog = doc
        for t in doc.get("tracks", []):
            if t.get("id") == tid:
                return t, None
        return None, NO_SUCH_TRACK

    def play_file(self, tid: str) -> tuple[Path | None, str | None, str | None]:
        """(local file, mime, problem) for a track - fetched into the cache once. Blocks until it is
        all there; `play_stream` is the one the deck uses."""
        t, why = self._track(tid)
        if t is None:
            return None, None, why
        key = t.get("video") or t.get("audio")
        if not key:
            return None, None, "That track has no file."
        local, out = self._store.fetch(key)
        if local is None:
            return None, None, out.problem
        self._touch(local)
        return local, str(t.get("mime") or "application/octet-stream"), None

    def play_stream(self, tid: str) -> dict:
        """What the play route needs: {'file': Path} when cached, else {'fetching': Fetching} once
        the headers came (the page plays while it arrives), else {'problem': sentence}."""
        t, why = self._track(tid)
        if t is None:
            return {"problem": why}
        key = t.get("video") or t.get("audio")
        if not key:
            return {"problem": "That track has no file."}
        mime = str(t.get("mime") or "application/octet-stream")
        cached = getattr(self._store, "cached", None)
        have = cached(key) if cached else None
        if have is not None:
            self._touch(have)
            return {"file": have, "mime": mime, "track": t}
        begin = getattr(self._store, "begin_fetch", None)
        if begin is None:
            local, out = self._store.fetch(key)
            if local is None:
                return {"problem": out.problem}
            return {"file": local, "mime": mime, "track": t}
        f = self._fetch_as_job(t, key)
        f.headed.wait(90)
        if f.error:
            return {"problem": f.error}
        if f.done.is_set() and f.final.exists():
            return {"file": f.final, "mime": mime, "track": t}
        if f.total is None:
            f.done.wait(600)
            if f.error or not f.final.exists():
                return {"problem": f.error or "The download did not finish."}
            return {"file": f.final, "mime": mime, "track": t}
        return {"fetching": f, "mime": mime, "track": t}

    def _fetch_as_job(self, t: dict, key: str):
        jobs = self._jobs
        job = None
        if jobs is not None and not any(j.note == key for j in jobs.running("fetch")):
            job = jobs.start("fetch", f"Fetching {t.get('title') or key}", note=key, progress=0.0,
                             cancel=lambda: None, cancel_note=CANCEL_FETCH, quiet=True)
        f = self._store.begin_fetch(key, cancelled=(lambda: bool(job and jobs.cancelled(job))) if job else None)
        if job is not None:
            def watch():
                last = -1
                while not f.done.wait(0.3):
                    if f.got != last:
                        last = f.got
                        jobs.progress(job, (f.got / f.total) if f.total else None, key)
                if f.error:
                    jobs.line(job, "[helix] " + f.error)
                jobs.finish(job, f.error is None, note=("Cached on this PC" if f.error is None else f.error))
                if f.error is None:
                    self.trim_cache()
            threading.Thread(target=watch, daemon=True, name="helix-radio-fetch-watch").start()
        return f

    def prefetch(self, tid: str) -> str | None:
        """Start caching a track in the background (the next one in the list). One sentence if not."""
        t, why = self._track(tid)
        if t is None:
            return why
        key = t.get("video") or t.get("audio")
        cached = getattr(self._store, "cached", None)
        begin = getattr(self._store, "begin_fetch", None)
        if not key or begin is None or (cached and cached(key)):
            return None
        self._fetch_as_job(t, key)
        return None

    def cache_all(self, by: str = "") -> tuple[dict | None, str | None]:
        """Cache every visible track on this PC, as one task with progress. Returns the task."""
        jobs = self._jobs
        if jobs is None:
            return None, "The task register is not available, so the station cannot be cached in the background."
        if jobs.running("cache"):
            return None, "The station is already being cached."
        doc, out = self._store.read_catalog()
        if doc is None:
            return None, out.problem
        cached = getattr(self._store, "cached", None)
        todo = [t for t in doc.get("tracks", []) if isinstance(t, dict) and not t.get("hidden")
                and (t.get("video") or t.get("audio")) and not (cached and cached(t.get("video") or t.get("audio")))]
        if not todo:
            return None, "Every track is already cached on this PC."
        job = jobs.start("cache", f"Caching the station - {len(todo)} tracks", by=by, progress=0.0,
                         cancel=lambda: None, cancel_note=CANCEL_CACHE)

        def run(job) -> bool:
            for i, t in enumerate(todo):
                if jobs.cancelled(job):
                    return False
                key = t.get("video") or t.get("audio")
                jobs.progress(job, i / len(todo), f"{t.get('title') or key} ({i + 1} of {len(todo)})")

                def on_progress(got, total, base=i / len(todo), span=1 / len(todo)):
                    if total:
                        jobs.progress(job, base + span * got / total)
                local, out = self._store.fetch(key, on_progress=on_progress, cancelled=lambda: jobs.cancelled(job))
                if local is None:
                    jobs.line(job, f"[helix] {t.get('title') or key}: {out.problem}")
                    if jobs.cancelled(job):
                        return False
                    continue
                jobs.line(job, f"cached {t.get('title') or key}")
            jobs.progress(job, 1.0, "Every track is on this PC")
            self.trim_cache()
            return True

        jobs.run(job, run, name="helix-radio-cache")
        return job.as_dict(), None

    def trim_cache(self) -> int:
        trim = getattr(self._store, "trim_cache", None)
        if trim is None:
            return 0
        try:
            return int(trim(int(self.cache_max_gb() * 1024 ** 3)))
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _touch(p: Path) -> None:
        try:
            os.utime(p, None)      # "last played" - the cache trims oldest-played first
        except OSError:
            pass
