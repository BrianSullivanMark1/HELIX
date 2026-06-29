"""KnowledgeService — the Forge's memory capability: the user's own documents and notes, searchable.

A Knowledge build is a named collection the orb and its agents can READ to answer from the user's own
material. It is a workspace build like any other (data/builds/<slug>/ with a .helixbuild.json manifest),
so it inherits git versioning, the rebuild-surviving guard skip, and voice rename/delete for free — but,
crucially, it is NOT produced by the coder. The user (or the orb on the user's behalf) ingests documents
and notes directly through this service, which sidesteps the build sandbox entirely: there is no coder
run, no escape guard, just files written into the base's own folder.

On disk, inside data/builds/<slug>/:
  - .helixbuild.json   the manifest (build_kind = knowledge), written by BuildService
  - knowledge.json     the document index (an array of KnowledgeDoc records)
  - docs/<id>.txt      the stored plain text of each ingested document (opaque internal filenames)

Retrieval is read-only and fenced as untrusted data (see helix/domain/knowledge.py) — exposed to the orb
AND to agents (it is deliberately NOT a BUILD_TOOL), while ingestion (create/remember) is a write path
reserved for the human-in-the-loop orb, never an autonomous agent.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from helix.domain.errors import BuildError
from helix.domain.events import BuildCreated, BuildDeleted, BuildIterated
from helix.domain.knowledge import (
    KnowledgeDoc,
    SearchHit,
    chunk_text,
    format_hits,
    rank_chunks,
    tokenize,
)
from helix.domain.models import App, BuildKind, slugify
from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.events import EventBus
from helix.ports.repo import VersionedRepo
from helix.services import attachments
from helix.services.builds import BuildService

_LOG = get_logger("knowledge")

INDEX_FILE = "knowledge.json"
DOCS_DIR = "docs"
_NONCE_BYTES = 4  # per-search fence nonce (matches the attachments bundler)


def _title_from_note(text: str) -> str:
    """A short, human title for a pasted note — its first non-empty line, trimmed."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:60] + ("…" if len(line) > 60 else "")
    return "Note"


def _new_nonce(index: int) -> str:
    """A fence nonce. Math.random/os.urandom-free determinism isn't required here, but the workflow
    sandbox forbids randomness in some contexts; a counter-salted token is unique-enough per call."""
    return f"{os.getpid():x}{index:04x}"


class KnowledgeService:
    def __init__(
        self, builds: BuildService, repo: VersionedRepo, clock: Clock, bus: EventBus | None = None
    ) -> None:
        self._builds = builds
        self._repo = repo
        self._clock = clock
        self._bus = bus
        # Ingestion is a read-modify-write of the per-base index; the orb (a worker thread) and the
        # Knowledge view (the UI thread) can both write, so serialize mutations. Search reads defensively.
        self._lock = threading.Lock()
        self._searches = 0  # salts the search fence nonce without needing randomness

    # ----- bases (a base is a workspace build with build_kind == KNOWLEDGE) -----
    def bases(self) -> list[App]:
        return [a for a in self._builds.list() if a.build_kind == BuildKind.KNOWLEDGE]

    def find(self, name: str) -> App | None:
        """Resolve a base by slug or case-insensitive display name (for the orb's by-voice ops)."""
        slug = slugify(name)
        target = (name or "").strip().lower()
        return next(
            (a for a in self.bases() if a.slug == slug or a.name.strip().lower() == target), None
        )

    def create(self, name: str, *, description: str | None = None) -> App:
        """Make a new, empty knowledge base. Idempotent for an existing base of the same name; refuses a
        name already taken by a DIFFERENT kind of build (so it can't silently alias onto an app/task)."""
        name = (name or "").strip()
        if not name:
            raise BuildError("Give the knowledge base a name.")
        slug = slugify(name)
        existing = next((a for a in self._builds.list() if a.slug == slug), None)
        if existing is not None:
            if existing.build_kind != BuildKind.KNOWLEDGE:
                raise BuildError(
                    f"There's already a {existing.build_kind.value} called '{existing.name}'. "
                    "Choose a different name for the knowledge base."
                )
            return existing  # already exists — reuse it
        app = App.from_request(
            name, description or "A collection of notes and documents HELIX can search."
        )
        app.build_kind = BuildKind.KNOWLEDGE
        self._builds.create_workspace(app)
        self._docs_dir(app.slug).mkdir(parents=True, exist_ok=True)
        self._save_index(app.slug, [])
        self._commit(app.slug, f"knowledge: create {app.name}")
        if self._bus is not None:
            self._bus.publish(BuildCreated(app))
        return app

    # ----- ingest -----
    def add_note(self, slug: str, text: str, *, title: str | None = None) -> KnowledgeDoc | None:
        """Save a pasted/typed/spoken note into a base. Returns the stored doc, or None if it was blank."""
        text = (text or "").strip()
        if not text:
            return None
        return self._store(slug, title or _title_from_note(text), text, source="note")

    def add_files(self, slug: str, paths: list) -> list[KnowledgeDoc]:
        """Ingest the readable text of the chosen files (binaries are skipped). Each becomes one doc."""
        return self._ingest_paths(slug, paths, source="file")

    def add_folder(self, slug: str, folder) -> list[KnowledgeDoc]:
        """Ingest the readable text files found under a folder (noise dirs + binaries are skipped)."""
        return self._ingest_paths(slug, [folder], source="folder")

    def _ingest_paths(self, slug: str, paths: list, *, source: str) -> list[KnowledgeDoc]:
        # collect_files expands folders, dedupes, caps the count, and filters binaries (by extension and a
        # NUL sniff) — the same vetting an attachment gets, reused so a Knowledge import behaves identically.
        files = attachments.collect_files([Path(p) for p in paths])
        out: list[KnowledgeDoc] = []
        for fp in files:
            text = self._read_text(fp)
            if text:
                doc = self._store(slug, fp.name, text, source=source)
                if doc is not None:
                    out.append(doc)
        return out

    @staticmethod
    def _read_text(path: Path) -> str:
        """Read a file as capped UTF-8 text (empty string if unreadable/empty)."""
        try:
            with path.open("rb") as fh:
                data = fh.read(attachments.MAX_FILE_BYTES + 1)
        except OSError:
            return ""
        truncated = len(data) > attachments.MAX_FILE_BYTES
        text = data[: attachments.MAX_FILE_BYTES].decode("utf-8", errors="replace").strip()
        if truncated:
            text += "\n… (truncated — file exceeds the per-file size limit)"
        return text

    def _store(self, slug: str, title: str, text: str, *, source: str) -> KnowledgeDoc | None:
        """Write one document's text + register it in the index. Serialized against concurrent writes."""
        if not self._builds.exists(slug):
            return None
        with self._lock:
            docs = self._load_index(slug)
            doc_id = self._next_id(docs)
            rel = f"{DOCS_DIR}/{doc_id}.txt"
            target = self._workspace(slug) / DOCS_DIR / f"{doc_id}.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_text(text, encoding="utf-8")
            except OSError as exc:
                _LOG.warning("could not store knowledge doc in %s: %s", slug, exc)
                return None
            doc = KnowledgeDoc(
                id=doc_id, title=title.strip() or "Untitled", source=source, file=rel,
                added_at=self._clock.now().isoformat(),
                bytes=len(text.encode("utf-8", errors="replace")),
            )
            docs.append(doc)
            self._save_index(slug, docs)
        self._commit(slug, f"knowledge: add {doc.title}")
        self._announce_change(slug)
        return doc

    # ----- read / manage -----
    def docs(self, slug: str) -> list[KnowledgeDoc]:
        return self._load_index(slug)

    def count(self, slug: str) -> int:
        return len(self._load_index(slug))

    def doc_text(self, slug: str, doc_id: str) -> str:
        """The stored text of one document (for the viewer's read pane)."""
        doc = next((d for d in self._load_index(slug) if d.id == doc_id), None)
        if doc is None:
            return ""
        try:
            return (self._workspace(slug) / doc.file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def remove_doc(self, slug: str, doc_id: str) -> bool:
        """Drop one document from a base (its stored text is deleted too). Returns True if removed."""
        with self._lock:
            docs = self._load_index(slug)
            doc = next((d for d in docs if d.id == doc_id), None)
            if doc is None:
                return False
            remaining = [d for d in docs if d.id != doc_id]
            self._save_index(slug, remaining)
            try:
                (self._workspace(slug) / doc.file).unlink()
            except OSError:
                pass  # index is the source of truth; an orphaned file is harmless and re-overwritten by id
        self._commit(slug, f"knowledge: remove {doc.title}")
        self._announce_change(slug)
        return True

    # ----- search (read-only; for the orb + agents via the search_knowledge tool) -----
    def search(self, query: str, base_name: str | None = None) -> str:
        """Find passages across the user's knowledge and return them as a fenced, untrusted-data block the
        model answers from. A friendly plain message when there's nothing to search or nothing matched."""
        query = (query or "").strip()
        if not query:
            return "Tell me what to look for in your knowledge."
        targets = self._resolve_targets(base_name)
        if isinstance(targets, str):
            return targets  # a friendly "no such base" / "no knowledge yet" message
        hits = self._gather_and_rank(query, targets)
        if not hits:
            where = f" in {targets[0].name}" if base_name and targets else " in your saved knowledge"
            return f"I couldn't find anything about that{where}."
        self._searches += 1
        return format_hits(hits, _new_nonce(self._searches))

    def auto_context(self, query: str, *, limit: int = 3, max_chars: int = 3000) -> str:
        """Ambient retrieval for the orb: if the user's message CLEARLY matches something they saved,
        return a fenced (speculative) block to surface it automatically — so HELIX answers from the user's
        own material without being told to search. Deliberately HIGH-PRECISION so most turns inject nothing:
          - the message must carry at least two distinct meaningful terms (greetings/chit-chat inject
            nothing; single-term recall is left to the explicit search_knowledge tool), and
          - a passage qualifies only if it covers MOST of those terms (at least two, and ≥60%) — so a
            single shared word never triggers it, while a stray extra word ("…the wifi password again?")
            doesn't suppress a genuine match.
        Returns "" when nothing qualifies. Cheap, local, no network — safe to call on every orb turn."""
        qtokens = list(dict.fromkeys(tokenize(query)))
        if len(qtokens) < 2:
            return ""
        bases = self.bases()
        if not bases:
            return ""
        qset = set(qtokens)
        strong: list[SearchHit] = []
        for h in self._gather_and_rank(query, bases):  # already ranked best-first
            htoks = set(tokenize(h.text))
            covered = sum(1 for t in qset if t in htoks)
            if covered >= 2 and covered / len(qset) >= 0.6:
                strong.append(h)
            if len(strong) >= limit:
                break
        if not strong:
            return ""
        self._searches += 1
        return format_hits(strong, _new_nonce(self._searches), max_chars=max_chars, speculative=True)

    def preview(self, query: str, base_name: str | None = None) -> list[SearchHit]:
        """Ranked hits for the Knowledge view's own search box (plain objects, not a fenced block)."""
        query = (query or "").strip()
        if not query:
            return []
        targets = self._resolve_targets(base_name)
        if isinstance(targets, str):
            return []
        return self._gather_and_rank(query, targets)

    def _resolve_targets(self, base_name: str | None):
        """The bases a search should cover: one named base, or all of them. Returns a friendly string
        instead of a list when there's nothing to search (no bases) or the named base doesn't exist."""
        bases = self.bases()
        if not bases:
            return "You haven't saved any knowledge yet. Tell me to remember something and I'll start a base."
        if base_name:
            one = self.find(base_name)
            if one is None:
                names = ", ".join(b.name for b in bases)
                return f"I don't have a knowledge base called '{base_name}'. You have: {names}."
            return [one]
        return bases

    def _gather_and_rank(self, query: str, bases: list[App]) -> list[SearchHit]:
        passages: list[tuple[str, str, str]] = []
        for base in bases:
            for doc in self._load_index(base.slug):
                try:
                    text = (self._workspace(base.slug) / doc.file).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
                for chunk in chunk_text(text):
                    passages.append((base.name, doc.title, chunk))
        return rank_chunks(query, passages)

    # ----- helpers -----
    def _workspace(self, slug: str) -> Path:
        return self._builds.workspace(slug)

    def _docs_dir(self, slug: str) -> Path:
        return self._workspace(slug) / DOCS_DIR

    def _index_path(self, slug: str) -> Path:
        return self._workspace(slug) / INDEX_FILE

    def _load_index(self, slug: str) -> list[KnowledgeDoc]:
        """Parse a base's document index, tolerating a missing/corrupt file (returns [])."""
        try:
            raw = json.loads(self._index_path(slug).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out: list[KnowledgeDoc] = []
        for d in raw if isinstance(raw, list) else []:
            if not isinstance(d, dict):
                continue
            did = str(d.get("id") or "").strip()
            rel = str(d.get("file") or "").strip()
            if not did or not rel:
                continue
            out.append(KnowledgeDoc(
                id=did, title=str(d.get("title") or "Untitled"), source=str(d.get("source") or "note"),
                file=rel, added_at=str(d.get("added_at") or ""), bytes=int(d.get("bytes") or 0),
            ))
        return out

    def _save_index(self, slug: str, docs: list[KnowledgeDoc]) -> None:
        """Write the index atomically (tmp + replace) so a concurrent search never reads a half-written
        file. The caller holds the lock for the surrounding read-modify-write."""
        data = [
            {"id": d.id, "title": d.title, "source": d.source, "file": d.file,
             "added_at": d.added_at, "bytes": d.bytes}
            for d in docs
        ]
        path = self._index_path(slug)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            _LOG.warning("could not save knowledge index for %s: %s", slug, exc)

    @staticmethod
    def _next_id(docs: list[KnowledgeDoc]) -> str:
        """A stable, unique, monotonic id — never derived from the user's filename (no path traversal)."""
        highest = 0
        for d in docs:
            try:
                highest = max(highest, int(d.id))
            except (TypeError, ValueError):
                continue
        return f"{highest + 1:04d}"

    def _commit(self, slug: str, message: str) -> None:
        """Version each change so the base has git history like every other build. Best-effort: a commit
        failure must never block the in-memory/on-disk change the user just made."""
        try:
            self._repo.commit_all(self._workspace(slug), message)
        except Exception:  # noqa: BLE001
            _LOG.debug("knowledge commit failed for %s", slug, exc_info=True)

    def _announce_change(self, slug: str) -> None:
        """Tell the UI a base's contents changed so the menu count + an open Knowledge view refresh."""
        if self._bus is None:
            return
        app = next((a for a in self.bases() if a.slug == slug), None)
        if app is not None:
            self._bus.publish(BuildIterated(app))

    def remember(self, note: str, base_name: str | None = None, *, default: str = "Notes") -> str:
        """The orb's by-voice capture: save a note, creating the named base (or a default 'Notes' base)
        if it doesn't exist yet. Returns a friendly confirmation for the model to relay."""
        note = (note or "").strip()
        if not note:
            return "What would you like me to remember?"
        target = (base_name or "").strip() or default
        try:
            base = self.create(target)  # idempotent if it already exists; refuses a cross-kind name clash
        except BuildError as exc:
            return str(exc)
        doc = self.add_note(base.slug, note)
        if doc is None:
            return "That note was empty, so I didn't save anything."
        return f"Saved that to {base.name}."
