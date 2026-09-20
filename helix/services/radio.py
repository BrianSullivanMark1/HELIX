"""HELIX RADIO - the company's music library, played from HELIX and (later) every app.

The catalog is one JSON in the bucket (docs/HELIX_RADIO.md section 4). This service is its only
writer from HELIX: add a track, hide a track, name a station. Playback hands the page a local
file (the bucket object, cached once) so seeking and video work without signed links.

Formats today: web-native ones are kept as they are; the rest are refused in one sentence until
the API's transcoder lands. Deleting is not here and will not be: hide, yes; delete, the GCS
console, a person.
"""
from __future__ import annotations

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

NOT_WEB = "That format needs the transcoder (coming with the radio API). For now: mp3, m4a, aac, ogg, wav, flac, mp4, webm."
UNKNOWN_EXT = "HELIX does not know that file type. Music: mp3, m4a, aac, ogg, wav, flac. Video: mp4, webm."
NO_SUCH_TRACK = "No track with that id."


def empty_catalog() -> dict:
    return {"version": 1, "stations": {"default": "HELIX RADIO"}, "tracks": []}


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


class RadioService:
    def __init__(self, store: RadioStore, *, station_override=None, clock=None) -> None:
        self._store = store
        self._override = station_override or (lambda: None)   # the local station name for this HELIX
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._catalog: dict | None = None

    # ---------------------------------------------------------------- reads
    def board(self, app: str = "default") -> dict:
        """What the deck shows: the catalog (visible tracks), the station name for this app, the
        bucket, and one sentence when something is off."""
        doc, out = self._store.read_catalog()
        if doc is None and out.problem == NO_CATALOG:
            doc, out = empty_catalog(), Outcome(True)      # an empty bucket is a radio with no songs yet
        with self._lock:
            self._catalog = doc
        tracks = [t for t in (doc or {}).get("tracks", []) if isinstance(t, dict) and not t.get("hidden")]
        stations = (doc or {}).get("stations") or {}
        return {
            "bucket": self._store.bucket(),
            "ok": out.ok,
            "problem": out.problem,
            "station": self.station_name(app, stations),
            "stations": stations,
            "tracks": tracks,
            "count": len(tracks),
        }

    def station_name(self, app: str, stations: dict | None = None) -> str:
        local = (self._override() or "").strip()
        if local:
            return local
        if stations is None:
            stations = (self._catalog or {}).get("stations") or {}
        return str(stations.get(app) or stations.get("default") or "HELIX RADIO")

    # ---------------------------------------------------------------- writes
    def add_track(self, local: Path, *, title: str, artist: str = "", theme: str = "", bpm: int | None = None,
                  uploaded_by: str = "") -> tuple[dict | None, str | None]:
        kind, mime_or_why = kind_of(local.name)
        if kind is None:
            return None, mime_or_why
        doc, out = self._store.read_catalog()
        if doc is None:
            if out.problem != NO_CATALOG:
                return None, out.problem
            doc = empty_catalog()
        tid = uuid.uuid4().hex[:6]
        ext = local.suffix.lower()
        key = f"{'videos' if kind == 'video' else 'tracks'}/{tid}{ext}"
        up = self._store.upload(local, key)
        if not up.ok:
            return None, up.problem
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
            "uploaded_by": clean_text(uploaded_by),
            "at": self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        doc.setdefault("tracks", []).append(track)
        wr = self._store.write_catalog(doc)
        if not wr.ok:
            return None, wr.problem
        with self._lock:
            self._catalog = doc
        return track, None

    def hide(self, tid: str) -> str | None:
        doc, out = self._store.read_catalog()
        if doc is None:
            return out.problem
        for t in doc.get("tracks", []):
            if t.get("id") == tid:
                t["hidden"] = True
                wr = self._store.write_catalog(doc)
                return None if wr.ok else wr.problem
        return NO_SUCH_TRACK

    def set_station(self, app: str, name: str) -> str | None:
        name = clean_text(name, 40)
        doc, out = self._store.read_catalog()
        if doc is None:
            if out.problem != NO_CATALOG:
                return out.problem
            doc = empty_catalog()
        stations = doc.setdefault("stations", {})
        key = (app or "default").strip() or "default"
        if name:
            stations[key] = name
        else:
            stations.pop(key, None) if key != "default" else None
        wr = self._store.write_catalog(doc)
        return None if wr.ok else wr.problem

    # ---------------------------------------------------------------- playback
    def play_file(self, tid: str) -> tuple[Path | None, str | None, str | None]:
        """(local file, mime, problem) for a track - fetched into the cache once."""
        with self._lock:
            doc = self._catalog
        if doc is None:
            doc, out = self._store.read_catalog()
            if doc is None:
                return None, None, out.problem
        for t in doc.get("tracks", []):
            if t.get("id") == tid:
                key = t.get("video") or t.get("audio")
                if not key:
                    return None, None, "That track has no file."
                local, out = self._store.fetch(key)
                if local is None:
                    return None, None, out.problem
                return local, str(t.get("mime") or "application/octet-stream"), None
        return None, None, NO_SUCH_TRACK
