"""Project folders — a hologram shelved under a project name on the menu (ARCHITECTURE.md §7a).

The folder is a tag in BuildService's sidecar (builds/.helixprojects.json), never a move on disk and
never a line in the committed manifest: it follows a rename to its new slug, survives finalize and a
version revert, dies with the build, and a mangled sidecar reads as "nothing filed". The orb files
by talking (file_hologram — fenced like rename_build, spoken as "Filing …"), the web face by a
button (POST /api/builds/{slug}/project) and renames folders (POST /api/projects/rename); both
regroup on BuildFiled. The maker's own auto-filing (an enclosure lands under its parts list) is
pinned in test_maker.py, beside the rig that builds one.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from helix.api.server import EventHub, build_app
from helix.domain.events import BuildFiled
from helix.domain.models import App, BuildKind
from helix.domain.vocabulary import friendly_tool_label
from helix.services.builds import PROJECTS, BuildService
from helix.services.conversation import BUILD_TOOLS
from helix.services.tools import ToolRegistry

# ---- the service ---------------------------------------------------------------------------------


class _NoRepo:
    def init(self, _ws) -> None: ...
    def commit_all(self, _ws, _msg) -> None: ...
    def revert_to(self, _ws, _sha) -> None: ...


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 6, 12, 0, 0)


def _svc(tmp_path) -> BuildService:
    return BuildService(tmp_path / "builds", _NoRepo(), _FixedClock())


def _make(svc: BuildService, name: str, kind: BuildKind = BuildKind.MODEL) -> App:
    app = App.from_request(name, "a thing")
    app.build_kind = kind
    svc.create_workspace(app)
    (svc.workspace(app.slug) / "index.html").write_text("<html></html>", encoding="utf-8")
    return svc.finalize(app)


def _shelves(svc: BuildService) -> list[tuple[str, list[str]]]:
    return [(f, [a.slug for a in apps]) for f, apps in svc.grouped(svc.list())]


def _sidecar(svc: BuildService) -> dict:
    return json.loads((svc.dir / PROJECTS).read_text(encoding="utf-8"))


def test_filing_groups_holograms_under_folders_and_the_loose_ones_last(tmp_path):
    svc = _svc(tmp_path)
    for name in ("Wall Cam Case", "Pi Mount", "Rover Chassis", "Loose Bracket"):
        _make(svc, name)
    assert _shelves(svc) == [("", ["loose-bracket", "pi-mount", "rover-chassis", "wall-cam-case"])]
    assert svc.projects() == []
    filed = svc.set_project("wall-cam-case", "  Wall   Camera ")
    assert filed is not None and filed.project == "Wall Camera"       # normalized, never raw
    svc.set_project("pi-mount", "Wall Camera")
    svc.set_project("rover-chassis", "rover")
    assert svc.projects() == ["rover", "Wall Camera"]                 # A–Z, case-insensitive
    assert _shelves(svc) == [
        ("rover", ["rover-chassis"]),
        ("Wall Camera", ["pi-mount", "wall-cam-case"]),
        ("", ["loose-bracket"]),
    ]
    assert svc.project_of("pi-mount") == "Wall Camera" and svc.project_of("loose-bracket") == ""
    # every listed build carries its folder — the faces read App.project, never the sidecar
    assert {a.slug: a.project for a in svc.list()}["wall-cam-case"] == "Wall Camera"
    # the sidecar is one small file at the builds root, and the manifest never learned the word
    assert _sidecar(svc)["folders"]["wall-cam-case"] == "Wall Camera"
    manifest_path = svc.workspace("wall-cam-case") / ".helixbuild.json"
    assert "project" not in json.loads(manifest_path.read_text(encoding="utf-8"))


def test_an_existing_folder_spelling_is_reused_and_blank_takes_it_out(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Case")
    _make(svc, "Lid")
    svc.set_project("case", "Wall Camera")
    assert svc.set_project("lid", "wall camera").project == "Wall Camera"  # spoken spelling folds
    assert svc.projects() == ["Wall Camera"]
    assert svc.set_project("lid", "   ").project == ""                     # blank = out of folder
    assert svc.set_project("lid", "").project == ""                        # again is not an error
    assert _shelves(svc) == [("Wall Camera", ["case"]), ("", ["lid"])]
    assert svc.set_project("ghost", "Anything") is None                    # no such build
    # the last build leaving a folder ends it
    svc.set_project("case", "")
    assert svc.projects() == [] and _shelves(svc) == [("", ["case", "lid"])]


def test_the_folder_follows_a_rename_and_survives_finalize_and_revert(tmp_path):
    svc = _svc(tmp_path)
    app = _make(svc, "Wall Cam Case")
    svc.set_project("wall-cam-case", "Wall Camera")
    renamed = svc.rename("wall-cam-case", "Camera Shell")
    assert renamed is not None and renamed.slug == "camera-shell"
    assert renamed.project == "Wall Camera"
    assert svc.project_of("camera-shell") == "Wall Camera" and svc.project_of("wall-cam-case") == ""
    # a re-build (the forge iterating: finalize on a fresh App) keeps the shelf
    again = App.from_request("Camera Shell", "make it taller")
    again.build_kind, again.created_at = BuildKind.MODEL, app.created_at
    assert svc.finalize(again).project == "Wall Camera"
    # so does rolling the design back a version — organization is not a version of the thing
    reverted = svc.revert("camera-shell", "abc12345")
    assert reverted is not None and reverted.project == "Wall Camera"


def test_the_tag_dies_with_the_build_so_a_same_slug_build_starts_loose(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Case")
    svc.set_project("case", "Rover")
    assert svc.delete("case")
    assert svc.projects() == []
    _make(svc, "Case")
    assert svc.project_of("case") == "" and svc.list()[0].project == ""


def test_renaming_a_folder_moves_every_build_and_merges_into_an_existing_one(tmp_path):
    svc = _svc(tmp_path)
    for name in ("A", "B", "C"):
        _make(svc, name)
    svc.set_project("a", "Rover")
    svc.set_project("b", "Rover")
    svc.set_project("c", "Wall Camera")
    assert svc.rename_project("rover", "Mars Rover") == 2         # the spoken spelling finds it
    assert svc.projects() == ["Mars Rover", "Wall Camera"]
    assert svc.rename_project("Mars Rover", "mars rover") == 2    # a case-only rename sticks
    assert svc.projects() == ["mars rover", "Wall Camera"]
    assert svc.rename_project("mars rover", "wall camera") == 2   # merge: the target's spelling
    assert svc.projects() == ["Wall Camera"]
    assert _shelves(svc) == [("Wall Camera", ["a", "b", "c"])]
    assert svc.rename_project("nope", "X") == 0 and svc.rename_project("Wall Camera", "  ") == 0


def test_a_mangled_sidecar_reads_as_nothing_filed_and_heals_on_the_next_filing(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Case")
    sidecar = svc.dir / PROJECTS
    sidecar.write_text("{not json", encoding="utf-8")
    assert svc.list()[0].project == "" and svc.projects() == []    # the menu still renders
    sidecar.write_text(json.dumps({"folders": ["a", "list"]}), encoding="utf-8")
    assert svc.projects() == []
    sidecar.write_text(json.dumps({"folders": {"case": 7, "x": "  "}}), encoding="utf-8")
    assert svc.projects() == []                                    # junk values are dropped
    assert svc.set_project("case", "Rover").project == "Rover"
    assert _sidecar(svc) == {"version": 1, "folders": {"case": "Rover"}}


def test_grouped_is_pure_and_case_folds_the_folder_key():
    a, b, c = App.from_request("A", "x"), App.from_request("B", "x"), App.from_request("C", "x")
    a.project, b.project = "Rover", "rover"
    groups = [(f, [x.slug for x in apps]) for f, apps in BuildService.grouped([c, a, b])]
    assert groups == [("Rover", ["a", "b"]), ("", ["c"])]
    assert BuildService.grouped([]) == [("", [])]


# ---- the tool -----------------------------------------------------------------------------------


class _B:
    def __init__(self, name, kind=BuildKind.MODEL, project=""):
        self.name = name
        self.slug = name.lower().replace(" ", "-")
        self.build_kind = kind
        self.request = "desc"
        self.project = project


class _Builds:
    """The BuildService surface file_hologram uses, over in-memory rows."""

    def __init__(self, *items):
        self._items = list(items)

    def list(self):
        return list(self._items)

    def projects(self):
        seen: dict[str, str] = {}
        for b in self._items:
            if b.project:
                seen.setdefault(b.project.casefold(), b.project)
        return sorted(seen.values(), key=str.casefold)

    def resolve_project(self, name):
        want = " ".join(name.split())
        return next((f for f in self.projects() if f.casefold() == want.casefold()), want)

    def set_project(self, slug, project):
        for b in self._items:
            if b.slug == slug:
                b.project = self.resolve_project(project)
                return b
        return None


class _Bus:
    def __init__(self):
        self.published = []

    def publish(self, e):
        self.published.append(e)

    def subscribe(self, *a):
        pass


class _Parts:
    def projects(self):
        return ["IronEye", "Wall Camera"]


class _Forge:
    def remove_build(self, name):
        return False


def _reg(*builds, parts=None):
    bus = _Bus()
    reg = ToolRegistry(_Forge(), _Builds(*builds), bus=bus, parts=parts)
    return reg, bus


def _file(reg, name, project=None) -> str:
    args = {"name": name} if project is None else {"name": name, "project": project}
    return reg.dispatch("file_hologram", args)


def _filed(bus) -> list[BuildFiled]:
    return [e for e in bus.published if isinstance(e, BuildFiled)]


def test_file_hologram_files_by_name_publishes_and_names_a_new_folder():
    reg, bus = _reg(_B("Wall Cam Case"), _B("Pi Mount"))
    out = _file(reg, "wall cam case", "Wall Camera")
    assert out == "Filed 'Wall Cam Case' under 'Wall Camera' — a new folder."
    filed = _filed(bus)
    assert len(filed) == 1 and filed[0].project == "Wall Camera"
    assert filed[0].app.slug == "wall-cam-case"
    # the second one reuses the folder's spelling, and says so without calling it new
    assert _file(reg, "Pi Mount", "wall camera") == "Filed 'Pi Mount' under 'Wall Camera'."
    # moving between folders says where it came from; filing where it already is changes nothing
    assert _file(reg, "Pi Mount", "Rover") == \
        "Filed 'Pi Mount' under 'Rover' (out of 'Wall Camera') — a new folder."
    assert _file(reg, "Pi Mount", "rover") == "'Pi Mount' is already in the 'Rover' folder."
    assert len(_filed(bus)) == 3


def test_file_hologram_borrows_the_parts_list_spelling_for_a_new_folder():
    # An enclosure and its BOM read as one project: 'ironeye' files under the list's 'IronEye'.
    reg, _bus = _reg(_B("Case"), parts=_Parts())
    assert _file(reg, "Case", "iron eye") == "Filed 'Case' under 'iron eye' — a new folder."
    assert _file(reg, "Case", "ironeye") == \
        "Filed 'Case' under 'IronEye' (out of 'iron eye') — a new folder."


def test_file_hologram_empty_or_a_no_word_takes_it_out():
    reg, bus = _reg(_B("Case", project="Rover"), _B("Lid", project="Rover"), _B("Loose"))
    assert _file(reg, "Case") == "Took 'Case' out of the 'Rover' folder."
    assert _file(reg, "Lid", "none") == "Took 'Lid' out of the 'Rover' folder."
    assert _file(reg, "Loose", "") == "'Loose' isn't in a folder."
    assert [e.project for e in _filed(bus)] == ["", ""]


def test_file_hologram_refuses_a_non_hologram_and_is_honest_about_unknown_names():
    reg, bus = _reg(_B("Tip Calc", kind=BuildKind.APP), _B("Morning", kind=BuildKind.TASK),
                    _B("Iron Man Mark VII"))
    refused = "'Tip Calc' is an app — only holograms go in project folders."
    assert _file(reg, "Tip Calc", "X") == refused
    assert _file(reg, "morning", "X") == \
        "'Morning' is a protocol — only holograms go in project folders."
    assert _file(reg, "ghost", "X").startswith("I don't see a hologram called 'ghost'")
    # a loose (contains) match still finds a hologram the user half-named
    filed = "Filed 'Iron Man Mark VII' under 'Suits' — a new folder."
    assert _file(reg, "mark vii", "Suits") == filed
    assert bus.published and isinstance(bus.published[-1], BuildFiled)


def test_file_hologram_is_offered_fenced_and_spoken():
    reg, _bus = _reg(_B("Case"))
    spec = next(s for s in reg.specs() if s.name == "file_hologram")
    assert spec.input_schema["required"] == ["name"]    # project may be left empty (= take it out)
    assert "file_hologram" in BUILD_TOOLS                 # a watcher never reorganizes the menu
    assert friendly_tool_label("file_hologram") == "Filing the hologram"
    assert friendly_tool_label("mcp__helix__file_hologram", {"name": "Case"}) == "Filing Case"


def test_list_apps_says_where_each_hologram_is_shelved():
    reg, _bus = _reg(_B("Case", project="Wall Camera"), _B("Loose"),
                     _B("Tip Calc", kind=BuildKind.APP))
    out = reg.dispatch("list_apps", {})
    assert "- Case [hologram, in Wall Camera]: desc" in out
    assert "- Loose [hologram]: desc" in out and "- Tip Calc [app]: desc" in out
    assert out.rstrip().endswith("Project folders: Wall Camera.")


# ---- the web face's endpoints --------------------------------------------------------------------


class _Settings:
    def __init__(self, **kv):
        self._d = dict(kv)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Shell:
    def __init__(self):
        self.pushed = []

    def snapshot(self):
        return {"t": "snapshot", "authed": True}

    def push(self, ev):
        self.pushed.append(ev)


def _api(tmp_path):
    svc = _svc(tmp_path)
    bus = _Bus()
    container = SimpleNamespace(settings=_Settings(web_token="tok-test"),
                                paths=SimpleNamespace(builds=str(svc.dir)), builds=svc, bus=bus)
    shell = _Shell()
    return build_app(container, shell, EventHub(), None), svc, bus, shell


def _post(app, path: str, body: dict) -> tuple[int, dict]:
    """One JSON POST straight through the ASGI stack (no test client — see test_api_lifecycle)."""
    payload = json.dumps(body).encode()
    headers = [(b"host", b"127.0.0.1:8737"), (b"x-helix-token", b"tok-test"),
               (b"content-type", b"application/json"),
               (b"content-length", str(len(payload)).encode())]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": "POST", "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8737),
    }
    status: list[int] = []
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(int(message["status"]))
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status[0], json.loads(b"".join(chunks) or b"{}")


def test_the_web_face_files_a_build_and_renames_folders(tmp_path):
    app, svc, bus, shell = _api(tmp_path)
    _make(svc, "Wall Cam Case")
    _make(svc, "Pi Mount")
    status, body = _post(app, "/api/builds/wall-cam-case/project", {"project": " wall  camera "})
    assert (status, body) == (200, {"ok": True, "project": "wall camera"})
    assert svc.project_of("wall-cam-case") == "wall camera"
    filed = _filed(bus)
    assert len(filed) == 1 and filed[0].app.slug == "wall-cam-case"
    assert filed[0].project == "wall camera"
    status, body = _post(app, "/api/builds/pi-mount/project", {"project": "Wall Camera"})
    assert body["project"] == "wall camera"                        # the existing spelling wins
    assert _post(app, "/api/builds/ghost/project", {"project": "X"})[0] == 404
    # the folder rename: every build in it moves, and every face is told to regroup
    rename = {"project": "wall camera", "name": "Wall Camera"}
    assert _post(app, "/api/projects/rename", rename) == (200, {"ok": True, "moved": 2})
    assert svc.projects() == ["Wall Camera"] and {"t": "builds"} in shell.pushed
    nope = {"project": "nope", "name": "X"}
    assert _post(app, "/api/projects/rename", nope)[1] == {"ok": False, "moved": 0}
    blank = {"project": "Wall Camera", "name": ""}
    assert _post(app, "/api/projects/rename", blank)[0] == 400
    # taking one out: an empty project
    status, body = _post(app, "/api/builds/pi-mount/project", {"project": ""})
    assert body == {"ok": True, "project": ""} and svc.project_of("pi-mount") == ""
