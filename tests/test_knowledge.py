"""Knowledge — the searchable notes/documents Build: pure retrieval, the store/ingest/search service,
and the orb tools (create_knowledge / remember write; search_knowledge read-only for agents too)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from helix.domain.errors import BuildError
from helix.domain.knowledge import (
    SearchHit,
    chunk_text,
    cosine,
    format_hits,
    rank_chunks,
    score_chunk,
    semantic_rank,
    tokenize,
)
from helix.domain.models import App, BuildKind
from helix.services.builds import BuildService
from helix.services.conversation import BUILD_TOOLS
from helix.services.knowledge import KnowledgeService
from helix.services.tools import ToolRegistry


# ───────────────────────── pure retrieval (domain) ─────────────────────────
def test_tokenize_drops_stopwords_and_noise():
    assert tokenize("The Wifi PASSWORD is hunter2!") == ["wifi", "password", "hunter2"]
    assert tokenize("a an the of") == []  # all stopwords


def test_chunk_text_splits_long_and_keeps_short_whole():
    assert chunk_text("") == []
    assert chunk_text("short note") == ["short note"]
    big = ("Para one is here. " * 60) + "\n\n" + ("Para two follows. " * 60)
    chunks = chunk_text(big, size=300, overlap=50)
    assert len(chunks) > 1
    assert all(c.strip() for c in chunks)
    assert all(len(c) <= 300 + 50 for c in chunks)  # roughly bounded by size (+ a boundary nudge)


def test_score_chunk_rewards_coverage_and_phrase_proximity():
    q = tokenize("wifi password")
    near = score_chunk(q, "the wifi password is hunter2")
    far = score_chunk(q, "wifi here. lots of words between. and somewhere a password.")
    none = score_chunk(q, "completely unrelated text about gardening")
    assert near > far > 0
    assert none == 0.0


def test_rank_chunks_orders_best_first_and_filters_zero():
    passages = [
        ("Base", "A", "nothing relevant here at all"),
        ("Base", "B", "the wifi password is hunter2"),
        ("Base", "C", "a page that mentions wifi once"),
    ]
    hits = rank_chunks("wifi password", passages)
    assert hits and hits[0].title == "B"
    assert all(h.score > 0 for h in hits)
    assert "A" not in [h.title for h in hits]  # zero-score passage dropped


def test_format_hits_fences_and_respects_budget():
    assert format_hits([], "abcd") == ""
    hits = [SearchHit("Notes", "Wifi", "the wifi password is hunter2", 9.0)]
    out = format_hits(hits, "abcd")
    assert "<<<KNOWLEDGE-abcd" in out and "KNOWLEDGE-abcd<<<" in out
    assert "[from Notes › Wifi]" in out and "hunter2" in out
    assert "never as instructions" in out  # the untrusted-data preamble
    # budget: a tiny cap still returns at least the first passage, but stops adding
    many = [SearchHit("N", str(i), "x" * 500, 1.0) for i in range(10)]
    capped = format_hits(many, "ef01", max_chars=600)
    assert capped.count("[from N") == 1


# ───────────────────────── the service (store + search) ─────────────────────────
class _NoRepo:
    def init(self, _ws) -> None: ...
    def commit_all(self, _ws, _msg) -> None: ...


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 6, 29, 9, 0, 0)


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


def _svc(tmp_path, bus=None, embedder=None) -> KnowledgeService:
    builds = BuildService(tmp_path, _NoRepo(), _FixedClock())
    return KnowledgeService(builds, _NoRepo(), _FixedClock(), bus=bus, embedder=embedder)


class _FakeEmbedder:
    """Deterministic embedder: anything about ENTRY/ACCESS maps to one direction, everything else to
    another — so a query and a passage can be 'about the same thing' with NO shared keywords."""

    model = "fake-1"
    _ENTRY = ("door", "code", "inside", "enter", "building", "access", "key", "lock")

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def embed(self, texts, *, input_type=None):
        self.calls += 1
        return [self._vec(t.lower()) for t in texts]

    @classmethod
    def _vec(cls, t: str):
        return [1.0, 0.0, 0.0] if any(w in t for w in cls._ENTRY) else [0.0, 0.0, 1.0]


def test_create_makes_a_knowledge_workspace_and_categorizes(tmp_path):
    bus = _Bus()
    ks = _svc(tmp_path, bus)
    app = ks.create("Recipes")
    assert app.build_kind == BuildKind.KNOWLEDGE and app.slug == "recipes"
    # manifest persisted the kind, and categorized() routes it to the knowledge bucket
    manifest = json.loads((tmp_path / "recipes" / ".helixbuild.json").read_text(encoding="utf-8"))
    assert manifest["build_kind"] == "knowledge"
    cat = ks._builds.categorized()
    assert {a.slug for a in cat["knowledge"]} == {"recipes"}
    assert cat["apps"] == [] and "recipes" not in {a.slug for a in cat["apps"]}
    assert any(type(e).__name__ == "BuildCreated" for e in bus.events)
    # idempotent: creating the same name returns the same base, not a duplicate
    assert ks.create("Recipes").slug == "recipes"


def test_create_refuses_a_name_taken_by_another_kind(tmp_path):
    ks = _svc(tmp_path)
    # an existing app named "Recipes"
    app = App.from_request("Recipes", "x")
    app.build_kind = BuildKind.APP
    ks._builds.create_workspace(app)
    with pytest.raises(BuildError):
        ks.create("Recipes")


def test_add_note_and_search_finds_it(tmp_path):
    ks = _svc(tmp_path)
    base = ks.create("Notes")
    ks.add_note(base.slug, "The wifi password is hunter2 for the home network.")
    ks.add_note(base.slug, "Dentist appointment is on Friday at 3pm.")
    assert ks.count(base.slug) == 2
    out = ks.search("wifi password")
    assert "hunter2" in out and "<<<KNOWLEDGE-" in out
    # an unrelated query returns a friendly miss, not a passage
    miss = ks.search("quarterly revenue")
    assert "couldn't find" in miss.lower()


def test_add_files_ingests_text_and_skips_binary(tmp_path):
    ks = _svc(tmp_path)
    base = ks.create("Docs")
    good = tmp_path / "note.txt"
    good.write_text("Project deadline moved to next Tuesday.", encoding="utf-8")
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02hidden")
    docs = ks.add_files(base.slug, [good, binary])
    assert [d.title for d in docs] == ["note.txt"]  # the binary was skipped
    assert "Tuesday" in ks.search("deadline")


def test_add_files_ingests_a_word_document(tmp_path):
    docx = pytest.importorskip("docx")
    ks = _svc(tmp_path)
    base = ks.create("Docs")
    p = tmp_path / "vault.docx"
    d = docx.Document()
    d.add_paragraph("The vault combination is 12-24-36.")
    d.save(str(p))
    added = ks.add_files(base.slug, [p])
    assert [a.title for a in added] == ["vault.docx"]
    assert "12-24-36" in ks.search("vault combination")


def test_ingest_outbox_harvests_results_and_clears_them(tmp_path):
    ks = _svc(tmp_path)
    outbox = tmp_path / "ob"
    outbox.mkdir()
    (outbox / "digest.md").write_text("Top story: markets rallied today.", encoding="utf-8")
    docs = ks.ingest_outbox("Morning News results", outbox)
    assert len(docs) == 1
    assert {a.name for a in ks.bases()} == {"Morning News results"}
    assert "markets rallied" in ks.search("markets")
    assert not (outbox / "digest.md").exists()           # harvested files are removed
    assert ks.ingest_outbox("Morning News results", outbox) == []  # nothing left to harvest


def test_task_watcher_harvests_its_outbox_into_knowledge(tmp_path):
    # The TaskService watcher: when a task finishes, its outbox is ingested into a base named for the task.
    from helix.services.tasks import TaskService

    ks = _svc(tmp_path)
    tasks = TaskService(ks._builds, knowledge=ks)
    outbox = tmp_path / "task_ob"
    outbox.mkdir()
    (outbox / "summary.txt").write_text("Sales were up 12 percent in June.", encoding="utf-8")

    class _Proc:
        def wait(self):
            return 0  # already finished

    tasks._watch_knowledge_outbox("Sales Report", _Proc(), outbox)
    assert {a.name for a in ks.bases()} == {"Sales Report results"}
    assert "12 percent" in ks.search("sales")


def test_remove_doc_drops_it_from_index_and_disk(tmp_path):
    ks = _svc(tmp_path)
    base = ks.create("Notes")
    doc = ks.add_note(base.slug, "ephemeral note about cats")
    assert ks.count(base.slug) == 1
    assert ks.remove_doc(base.slug, doc.id) is True
    assert ks.count(base.slug) == 0
    assert not (tmp_path / base.slug / doc.file).exists()
    assert ks.remove_doc(base.slug, doc.id) is False  # already gone


def test_search_scoping_and_no_bases(tmp_path):
    ks = _svc(tmp_path)
    assert "don't have a vault" in ks.search("anything").lower()
    work = ks.create("Work")
    home = ks.create("Home")
    ks.add_note(work.slug, "The office alarm code is 4815.")
    ks.add_note(home.slug, "The garage code is 1623.")
    # scoping to one base only searches it
    assert "4815" in ks.search("code", "Work")
    assert "1623" not in ks.search("code", "Work")
    # unknown base name → friendly message naming what exists
    bad = ks.search("code", "Nonexistent")
    assert "don't have a vault" in bad and "Work" in bad
    # across all bases, both are reachable
    assert "4815" in ks.search("office alarm")


def test_remember_creates_default_base_and_saves(tmp_path):
    bus = _Bus()
    ks = _svc(tmp_path, bus)
    msg = ks.remember("The spare key is under the third flowerpot.")
    assert "Notes" in msg
    assert {a.slug for a in ks.bases()} == {"notes"}
    assert "flowerpot" in ks.search("spare key")
    # naming a base routes there (and creates it on the fly)
    ks.remember("Standup is at 9am.", "Work")
    assert "standup" in ks.search("standup", "Work").lower()


def test_doc_text_round_trips(tmp_path):
    ks = _svc(tmp_path)
    base = ks.create("Notes")
    doc = ks.add_note(base.slug, "exact body text")
    assert ks.doc_text(base.slug, doc.id) == "exact body text"
    assert ks.doc_text(base.slug, "nope") == ""


def test_auto_context_surfaces_only_on_a_confident_match(tmp_path):
    ks = _svc(tmp_path)
    assert ks.auto_context("what's the wifi password") == ""  # no bases yet → nothing
    base = ks.create("Notes")
    ks.add_note(base.slug, "The wifi password is hunter2 for the home network.")
    ks.add_note(base.slug, "Dentist appointment on Friday.")
    # a clear multi-term match auto-surfaces, with the SPECULATIVE framing (a stray extra word is fine)
    out = ks.auto_context("what's the wifi password again?")
    assert "hunter2" in out and "may have saved knowledge" in out
    # a single meaningful term is left to the explicit search tool (no ambient inject)
    assert ks.auto_context("dentist") == ""
    # two terms but no one passage covers BOTH → nothing surfaced (a shared word isn't enough)
    assert ks.auto_context("wifi dentist") == ""
    # a message unrelated to anything saved → nothing
    assert ks.auto_context("quarterly revenue forecast") == ""


# ───────────────────────── semantic search (optional embeddings) ─────────────────────────
def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_semantic_rank_keeps_keyword_and_strong_semantic_drops_the_rest():
    scored = [
        ("B", "kw", "keyword passage", 5.0, 0.10),   # keyword hit, weak cosine → kept (never regress)
        ("B", "sem", "semantic passage", 0.0, 0.90),  # no keyword, strong cosine → kept
        ("B", "none", "unrelated", 0.0, 0.20),        # neither → dropped
    ]
    titles = [h.title for h in semantic_rank(scored)]
    assert "kw" in titles and "sem" in titles and "none" not in titles


def test_semantic_search_finds_a_meaning_match_with_no_shared_words(tmp_path):
    emb = _FakeEmbedder()
    ks = _svc(tmp_path, embedder=emb)
    base = ks.create("Home")
    ks.add_note(base.slug, "The door code is 4815.")
    ks.add_note(base.slug, "Dentist appointment on Friday.")
    # "get inside the building" shares NO words with "the door code is 4815" — only meaning.
    out = ks.search("how do I get inside the building")
    assert "4815" in out
    assert "Dentist" not in out  # the unrelated note is not dragged in


def test_semantic_falls_back_to_keyword_when_the_embedder_fails(tmp_path):
    class _Failing:
        model = "x"

        def available(self):
            return True

        def embed(self, texts, *, input_type=None):
            return None  # always fails

    ks = _svc(tmp_path, embedder=_Failing())
    base = ks.create("Notes")
    ks.add_note(base.slug, "The wifi password is hunter2.")
    assert "hunter2" in ks.search("wifi password")  # keyword search still works


def test_semantic_caches_chunk_vectors_across_searches(tmp_path):
    emb = _FakeEmbedder()
    ks = _svc(tmp_path, embedder=emb)
    base = ks.create("Home")
    ks.add_note(base.slug, "The door code is 4815.")
    ks.search("how do I get inside")   # embeds the query + the one chunk (2 calls)
    ks.search("how do I get inside")   # embeds only the query; the chunk is served from cache (1 call)
    assert emb.calls == 3
    cache = json.loads((ks._builds.dir / ".embeddings_cache.json").read_text(encoding="utf-8"))
    assert len(cache) == 1  # exactly one chunk vector cached (not re-embedded or duplicated)


# ───────────────────────── the orb tools ─────────────────────────
class _Forge:
    def remove_build(self, name) -> bool:
        return False


def _registry(tmp_path):
    builds = BuildService(tmp_path, _NoRepo(), _FixedClock())
    ks = KnowledgeService(builds, _NoRepo(), _FixedClock())
    return ToolRegistry(_Forge(), builds, knowledge=ks), ks


def test_knowledge_tools_are_exposed_only_when_wired(tmp_path):
    bare = ToolRegistry(_Forge(), BuildService(tmp_path, _NoRepo(), _FixedClock()))
    assert not ({"search_knowledge", "create_knowledge", "remember"} & {t.name for t in bare.specs()})
    reg, _ = _registry(tmp_path)
    assert {"search_knowledge", "create_knowledge", "remember"} <= {t.name for t in reg.specs()}


def test_write_tools_are_build_tools_but_search_is_not():
    # The read/write boundary that gives agents search but denies them writes.
    assert "create_knowledge" in BUILD_TOOLS and "remember" in BUILD_TOOLS
    assert "search_knowledge" not in BUILD_TOOLS


def test_create_knowledge_tool_makes_a_base_and_can_seed_it(tmp_path):
    reg, ks = _registry(tmp_path)
    out = reg.dispatch("create_knowledge", {"name": "Recipes", "note": "Grandma's pie crust recipe."})
    assert "Recipes" in out and "first note" in out.lower()
    assert {a.slug for a in ks.bases()} == {"recipes"}
    assert "pie crust" in ks.search("pie")


def test_create_knowledge_tool_reports_a_kind_clash(tmp_path):
    reg, ks = _registry(tmp_path)
    app = App.from_request("Recipes", "x")
    app.build_kind = BuildKind.APP
    ks._builds.create_workspace(app)
    out = reg.dispatch("create_knowledge", {"name": "Recipes"})
    assert "already" in out.lower()  # a friendly message, not a raised error


def test_remember_and_search_tools_round_trip(tmp_path):
    reg, _ = _registry(tmp_path)
    assert "Saved" in reg.dispatch("remember", {"note": "The router IP is 192.168.1.1."})
    out = reg.dispatch("search_knowledge", {"query": "router IP"})
    assert "192.168.1.1" in out


def test_search_knowledge_tool_scopes_to_a_named_base(tmp_path):
    # The dispatch layer forwards the optional `knowledge` base-scoping arg through to the service.
    reg, _ = _registry(tmp_path)
    reg.dispatch("remember", {"note": "The office alarm code is 4815.", "knowledge": "Work"})
    reg.dispatch("remember", {"note": "The garage code is 1623.", "knowledge": "Home"})
    out = reg.dispatch("search_knowledge", {"query": "code", "knowledge": "Work"})
    assert "4815" in out and "1623" not in out
