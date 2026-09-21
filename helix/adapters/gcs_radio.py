"""The radio's bucket, through the user's own gcloud login (DESKTOP). Same trust as the fleet read:
no keys, no secrets in HELIX; whatever your gcloud can do to the bucket, the radio can.

Reads the catalog, writes it back, copies a file up, copies a file down into the local cache.
Every call returns a plain sentence on failure and never raises. ASCII in everything it prints.

TWO WAYS TO MOVE BYTES. The catalog (a small JSON) goes through the gcloud CLI as before. The
media - songs and videos, up to hundreds of MB - go over plain HTTPS to the bucket with a token
from `gcloud auth print-access-token` (the same login, no console window, no CLI start-up per
file): a stream DOWN into the cache that the page can play while it is still arriving, and a
RESUMABLE upload UP in 8 MB pieces so progress is real and a cancel between pieces leaves nothing
behind. When the token cannot be had, the CLI copy is the fallback (no progress, still correct).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from helix.adapters.gcloud_fleet import Runner, _real_runner, classify, NOT_INSTALLED
from helix.ports.radio import BAD_CATALOG, NO_BUCKET, NO_CATALOG, Outcome

CANCELLED = "Stopped by the person."
PIECE = 8 * 1024 * 1024
TOKEN_TTL_S = 40 * 60
STORAGE = "https://storage.googleapis.com"

Progress = Callable[[int, int | None], None]     # (bytes so far, total or None)
Cancelled = Callable[[], bool]


@dataclass
class Fetching:
    """A download in flight: the growing .part file, the total once the headers came, done/error."""
    key: str
    part: Path
    final: Path
    total: int | None = None
    got: int = 0
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    headed: threading.Event = field(default_factory=threading.Event)


def _http_error_sentence(e: urllib.error.HTTPError, what: str) -> str:
    if e.code in (401, 403):
        return f"Your gcloud account is not allowed to {what} in this bucket."
    if e.code == 404:
        return "That file is not in the bucket."
    return f"The bucket answered {e.code} while trying to {what}."


class GcsRadio:
    def __init__(self, bucket: Callable[[], str | None], *, runner: Runner | None = None,
                 gcloud: str = "gcloud", cache_dir: Path | None = None, opener=None) -> None:
        self._bucket = bucket
        self._run = runner or _real_runner
        self._gcloud = shutil.which(gcloud) or gcloud
        self._cache = cache_dir or Path(tempfile.gettempdir()) / "helix_radio"
        self._open = opener or urllib.request.urlopen
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._inflight: dict[str, Fetching] = {}

    # ---------------------------------------------------------------- plumbing
    def bucket(self) -> str | None:
        b = (self._bucket() or "").strip().removeprefix("gs://").strip("/")
        return b or None

    def cache_dir(self) -> Path:
        return self._cache

    def available(self) -> tuple[bool, str | None]:
        if not self.bucket():
            return False, NO_BUCKET
        ran = self._run([self._gcloud, "storage", "ls", f"gs://{self.bucket()}/"], 25.0)
        bad = classify(ran)
        return (True, None) if bad is None else (False, bad[0])

    def _uri(self, key: str) -> str:
        return f"gs://{self.bucket()}/{key.lstrip('/')}"

    def _object_url(self, key: str) -> str:
        return f"{STORAGE}/storage/v1/b/{urllib.parse.quote(self.bucket() or '', safe='')}/o/{urllib.parse.quote(key.lstrip('/'), safe='')}?alt=media"

    def token(self, *, fresh: bool = False) -> str | None:
        """An access token for the user's own login, cached; None when gcloud will not give one."""
        with self._lock:
            if not fresh and self._token and time.time() < self._token[1]:
                return self._token[0]
        ran = self._run([self._gcloud, "auth", "print-access-token"], 30.0)
        tok = (ran.out or "").strip().splitlines()[0].strip() if ran.rc == 0 and (ran.out or "").strip() else ""
        if not tok:
            return None
        with self._lock:
            self._token = (tok, time.time() + TOKEN_TTL_S)
        return tok

    # ---------------------------------------------------------------- the catalog
    def read_catalog(self) -> tuple[dict | None, Outcome]:
        if not self.bucket():
            return None, Outcome(False, NO_BUCKET)
        ran = self._run([self._gcloud, "storage", "cat", self._uri("catalog.json")], 30.0)
        if ran.rc != 0:
            low = ((ran.err or "") + (ran.out or "")).lower()
            if "not found" in low or "no such object" in low or "404" in low or "matched no objects" in low:
                return None, Outcome(False, NO_CATALOG)
            bad = classify(ran) or (NOT_INSTALLED, "")
            return None, Outcome(False, bad[0], bad[1])
        try:
            doc = json.loads(ran.out or "{}")
        except ValueError:
            return None, Outcome(False, BAD_CATALOG, (ran.out or "")[:200])
        if not isinstance(doc, dict) or not isinstance(doc.get("tracks", []), list):
            return None, Outcome(False, BAD_CATALOG)
        return doc, Outcome(True)

    def write_catalog(self, doc: dict) -> Outcome:
        if not self.bucket():
            return Outcome(False, NO_BUCKET)
        self._cache.mkdir(parents=True, exist_ok=True)
        tmp = self._cache / "catalog.upload.json"
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")   # ASCII-safe: json escapes
        ran = self._run([self._gcloud, "storage", "cp", str(tmp), self._uri("catalog.json")], 40.0)
        bad = classify(ran)
        return Outcome(True) if bad is None else Outcome(False, bad[0], bad[1])

    # ---------------------------------------------------------------- files: up
    def upload(self, local: Path, key: str, *, mime: str = "application/octet-stream",
               on_progress: Progress | None = None, cancelled: Cancelled | None = None) -> Outcome:
        if not self.bucket():
            return Outcome(False, NO_BUCKET)
        tok = self.token()
        if tok is None:
            return self._upload_cli(local, key)
        total = local.stat().st_size
        start_url = (f"{STORAGE}/upload/storage/v1/b/{urllib.parse.quote(self.bucket() or '', safe='')}/o"
                     f"?uploadType=resumable&name={urllib.parse.quote(key.lstrip('/'), safe='')}")
        try:
            req = urllib.request.Request(start_url, data=b"{}", method="POST", headers={
                "Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(total)})
            with self._open(req, timeout=60) as resp:
                session = resp.headers.get("Location")
        except urllib.error.HTTPError as e:
            return Outcome(False, _http_error_sentence(e, "upload"))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return Outcome(False, f"Could not reach the bucket to start the upload: {e}")
        if not session:
            return Outcome(False, "The bucket did not open an upload session.")
        sent = 0
        with local.open("rb") as fh:
            while sent < total:
                if cancelled and cancelled():
                    return Outcome(False, CANCELLED)
                piece = fh.read(PIECE)
                end = sent + len(piece) - 1
                req = urllib.request.Request(session, data=piece, method="PUT", headers={
                    "Content-Type": mime, "Content-Range": f"bytes {sent}-{end}/{total}"})
                try:
                    with self._open(req, timeout=300) as resp:
                        status = resp.status
                except urllib.error.HTTPError as e:
                    if e.code == 308:
                        status = 308
                    else:
                        return Outcome(False, _http_error_sentence(e, "upload"))
                except (urllib.error.URLError, OSError, TimeoutError) as e:
                    return Outcome(False, f"The upload broke off at {sent} of {total} bytes: {e}")
                sent = end + 1
                if on_progress:
                    on_progress(sent, total)
                if status not in (200, 201, 308):
                    return Outcome(False, f"The bucket answered {status} mid-upload.")
        return Outcome(True)

    def _upload_cli(self, local: Path, key: str) -> Outcome:
        ran = self._run([self._gcloud, "storage", "cp", str(local), self._uri(key)], 600.0)
        bad = classify(ran)
        return Outcome(True) if bad is None else Outcome(False, bad[0], bad[1])

    # ---------------------------------------------------------------- files: down
    def cached(self, key: str) -> Path | None:
        local = self._cache / key.replace("/", "__")
        return local if local.exists() and local.stat().st_size > 0 else None

    def fetch(self, key: str, *, on_progress: Progress | None = None, cancelled: Cancelled | None = None) -> tuple[Path | None, Outcome]:
        """The object as a local file in the cache (downloaded once; the cache is by key). Blocks."""
        if not self.bucket():
            return None, Outcome(False, NO_BUCKET)
        have = self.cached(key)
        if have is not None:
            if on_progress:
                on_progress(have.stat().st_size, have.stat().st_size)
            return have, Outcome(True)
        f = self.begin_fetch(key, cancelled=cancelled)
        last = -1
        while not f.done.wait(0.25):
            if on_progress and f.got != last:
                last = f.got
                on_progress(f.got, f.total)
        if f.error:
            return None, Outcome(False, f.error)
        if on_progress:
            on_progress(f.got, f.total or f.got)
        return f.final, Outcome(True)

    def begin_fetch(self, key: str, *, cancelled: Cancelled | None = None) -> Fetching:
        """Start (or join) a download of `key` into the cache; returns at once. The .part file grows
        as bytes arrive; `headed` fires once the total is known; `done` when it is on disk or failed."""
        self._cache.mkdir(parents=True, exist_ok=True)
        final = self._cache / key.replace("/", "__")
        with self._lock:
            live = self._inflight.get(key)
            if live is not None and not live.done.is_set():
                return live
            f = Fetching(key=key, part=final.with_suffix(final.suffix + ".part"), final=final)
            self._inflight[key] = f
        threading.Thread(target=self._download, args=(f, cancelled), daemon=True, name="helix-radio-fetch").start()
        return f

    def _download(self, f: Fetching, cancelled: Cancelled | None) -> None:
        try:
            if not self.bucket():
                f.error = NO_BUCKET
                return
            tok = self.token()
            if tok is None:
                self._download_cli(f)
                return
            req = urllib.request.Request(self._object_url(f.key), headers={"Authorization": f"Bearer {tok}"})
            try:
                resp = self._open(req, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    tok = self.token(fresh=True)
                    if tok is None:
                        f.error = "gcloud would not give a token for the bucket."
                        return
                    req = urllib.request.Request(self._object_url(f.key), headers={"Authorization": f"Bearer {tok}"})
                    try:
                        resp = self._open(req, timeout=60)
                    except urllib.error.HTTPError as e2:
                        f.error = _http_error_sentence(e2, "read")
                        return
                else:
                    f.error = _http_error_sentence(e, "read")
                    return
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                f.error = f"Could not reach the bucket: {e}"
                return
            with resp:
                length = resp.headers.get("Content-Length")
                f.total = int(length) if length and length.isdigit() else None
                f.headed.set()
                with f.part.open("wb") as out:
                    while True:
                        if cancelled and cancelled():
                            f.error = CANCELLED
                            return
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        out.flush()
                        f.got += len(chunk)
            if f.total is not None and f.got != f.total:
                f.error = f"The download stopped short ({f.got} of {f.total} bytes)."
                return
            self._settle(f)
        except Exception as e:  # noqa: BLE001 - never raise out of the thread
            f.error = f"The download failed: {type(e).__name__}: {e}"
        finally:
            if f.error:
                try:
                    f.part.unlink(missing_ok=True)
                except OSError:
                    pass
            f.headed.set()
            f.done.set()
            with self._lock:
                if self._inflight.get(f.key) is f:
                    self._inflight.pop(f.key, None)

    def _download_cli(self, f: Fetching) -> None:
        ran = self._run([self._gcloud, "storage", "cp", self._uri(f.key), str(f.part)], 600.0)
        bad = classify(ran)
        if bad is not None:
            f.error = bad[0]
            return
        if not f.part.exists():
            f.error = "gcloud said it copied the file, but it is not on disk."
            return
        f.got = f.total = f.part.stat().st_size
        self._settle(f)

    @staticmethod
    def _settle(f: Fetching) -> None:
        """.part -> final. On Windows a reader may hold the .part for a moment (the play route reads
        it as it grows); the rename is retried briefly instead of failing the whole download."""
        last: OSError | None = None
        for _ in range(40):
            try:
                f.part.replace(f.final)
                return
            except OSError as e:
                last = e
                time.sleep(0.1)
        f.error = f"The download finished but the file could not be settled in the cache: {last}"

    # ---------------------------------------------------------------- the cache's size
    def cache_report(self) -> dict:
        files = [p for p in self._cache.glob("*") if p.is_file() and not p.name.endswith(".part") and not p.name.endswith(".json")]
        size = sum(p.stat().st_size for p in files)
        return {"dir": str(self._cache), "files": len(files), "bytes": size}

    def trim_cache(self, max_bytes: int, keep: set[str] | None = None) -> int:
        """Oldest-played first, down to max_bytes. Returns the bytes freed. Never touches .part files."""
        files = sorted((p for p in self._cache.glob("*") if p.is_file() and not p.name.endswith(".part") and not p.name.endswith(".json")),
                       key=lambda p: p.stat().st_mtime)   # the service touches a file on every play
        total = sum(p.stat().st_size for p in files)
        freed = 0
        for p in files:
            if total <= max_bytes:
                break
            if keep and p.name in keep:
                continue
            try:
                n = p.stat().st_size
                p.unlink()
                total -= n
                freed += n
            except OSError:
                continue
        return freed
