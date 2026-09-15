"""The SAP faculty's routes, as plain ASGI — helix/api/server.py's '# ----- the SAP faculty -----'
block. Every route is a thin call into SapService's panel-facing dicts, so the tests check the
plumbing, not the facts: the query and body reach the service as the right arguments, the
service's dict comes back untouched, an unknown table is a 404, a container without the faculty
answers 503 with a sentence, and a service that raises answers a JSON error rather than a
traceback (the page renders `error` as text; a bare 500 would leave it blank).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

from helix.api.server import EventHub, build_app


class _Settings:
    def __init__(self, **kv):
        self._d = dict(kv)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _FakeSap:
    """Records every call and answers with a recognisable dict, so a test can assert both the
    arguments the route passed and that the route returned the service's answer as-is."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.tables = {"AFRU": {"name": "AFRU", "description": "Order Confirmations",
                                "keys": ["MANDT", "RUECK", "RMZHL"], "fields": [], "joins": []}}
        self.boom: set[str] = set()  # method names that raise

    def _hit(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.boom:
            raise ValueError(f"{name} fell over")

    def status_dict(self):
        self._hit("status_dict")
        return {"shipped": 12, "curated_joins": 30, "verified_joins": 28, "edw_tables_recorded": 3}

    def search_dict(self, query, *, limit=30):
        self._hit("search_dict", query, limit=limit)
        return {"hits": [{"table": "AFRU", "field": "BUDAT", "description": "Posting date"}],
                "tables": [{"name": "AFRU", "description": "Order Confirmations", "module": "PP"}]}

    def table_dict(self, name, *, fetch=True):
        self._hit("table_dict", name, fetch=fetch)
        return self.tables.get(name.upper())

    def join_dict(self, tables, *, root=""):
        self._hit("join_dict", list(tables), root=root)
        return {"plan": [{"left": "AFRU", "right": "AFVC",
                          "on": [["AUFPL", "AUFPL"], ["APLZL", "APLZL"]]}],
                "filters": [], "unreachable": [], "sql": "FROM AFRU a\nJOIN AFVC b …", "edw": {}}

    def sql_dict(self, spec):
        self._hit("sql_dict", spec)
        return {"sql": "SELECT 1", "warnings": [], "notes": [], "columns": []}

    def report_dict(self, name="wip"):
        self._hit("report_dict", name)
        return {"columns": [{"label": "Order", "source": "AUFK.AUFNR", "status": "standard"}],
                "sql": "SELECT …", "warnings": []}

    def edw_dict(self):
        self._hit("edw_dict")
        return {"prefix": "EDW.SRC_SAPECC_ARP.", "view_prefix": "TV_", "plant": "1006",
                "tables": []}

    def edw_record(self, action, *, table="", text=""):
        self._hit("edw_record", action, table=table, text=text)
        return {"ok": True, "message": f"Recorded {table}."}


def _app(sap):
    container = SimpleNamespace(settings=_Settings(web_token="tok-test"),
                                paths=SimpleNamespace(builds="does-not-exist"), sap=sap)
    return build_app(container, SimpleNamespace(), EventHub(), None)


def _call(app, method, url, body: bytes = b"", content_type: str | None = None, token="tok-test"):
    """One request through the ASGI app, no server: `url` may carry a query string."""
    parts = urlsplit(url)
    headers = [(b"host", b"127.0.0.1:8737")]
    if token:
        headers.append((b"x-helix-token", token.encode()))
    if content_type:
        headers.append((b"content-type", content_type.encode()))
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": method, "path": parts.path, "raw_path": parts.path.encode(), "root_path": "",
        "query_string": parts.query.encode(), "headers": headers,
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8737),
    }
    out = {"status": 0, "body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = int(message["status"])
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], json.loads(out["body"]) if out["body"] else None


def _post(app, url, payload):
    return _call(app, "POST", url, json.dumps(payload).encode(), "application/json")


# ---- the reads ------------------------------------------------------------------------------

def test_the_status_route_returns_the_services_dict_untouched():
    sap = _FakeSap()
    status, data = _call(_app(sap), "GET", "/api/sap/status")
    assert status == 200 and data == sap.status_dict()


def test_a_search_carries_the_query_and_a_clamped_limit():
    sap = _FakeSap()
    app = _app(sap)
    status, data = _call(app, "GET", "/api/sap/search?q=posting%20date&limit=5")
    assert status == 200 and data["hits"][0]["field"] == "BUDAT"
    assert data["tables"][0]["name"] == "AFRU"
    assert sap.calls[-1] == ("search_dict", ("posting date",), {"limit": 5})
    _call(app, "GET", "/api/sap/search")               # no query: an empty search, default limit
    assert sap.calls[-1] == ("search_dict", ("",), {"limit": 30})
    _call(app, "GET", "/api/sap/search?q=x&limit=0")   # a nonsense limit is clamped, not refused
    assert sap.calls[-1][2] == {"limit": 1}
    _call(app, "GET", "/api/sap/search?q=x&limit=9999")
    assert sap.calls[-1][2] == {"limit": 200}


def test_a_known_table_is_read_with_its_fetch_flag():
    sap = _FakeSap()
    app = _app(sap)
    status, data = _call(app, "GET", "/api/sap/table/AFRU")
    assert status == 200 and data["keys"] == ["MANDT", "RUECK", "RMZHL"]
    assert sap.calls[-1] == ("table_dict", ("AFRU",), {"fetch": True})
    _call(app, "GET", "/api/sap/table/AFRU?fetch=false")
    assert sap.calls[-1] == ("table_dict", ("AFRU",), {"fetch": False})


def test_an_unknown_table_is_a_404_with_a_sentence():
    sap = _FakeSap()
    status, data = _call(_app(sap), "GET", "/api/sap/table/MDTB")
    assert status == 404 and "MDTB" in data["error"]
    assert sap.calls[-1] == ("table_dict", ("MDTB",), {"fetch": True})


def test_a_join_splits_the_comma_list_and_carries_the_root():
    sap = _FakeSap()
    app = _app(sap)
    status, data = _call(app, "GET", "/api/sap/join?tables=AFRU,%20AFVC,,CRHD&root=AFVC")
    assert status == 200 and data["plan"][0]["on"] == [["AUFPL", "AUFPL"], ["APLZL", "APLZL"]]
    assert sap.calls[-1] == ("join_dict", (["AFRU", "AFVC", "CRHD"],), {"root": "AFVC"})
    _call(app, "GET", "/api/sap/join")                 # nothing named: an empty plan request
    assert sap.calls[-1] == ("join_dict", ([],), {"root": ""})


def test_the_report_route_names_the_report():
    sap = _FakeSap()
    status, data = _call(_app(sap), "GET", "/api/sap/report/wip")
    assert status == 200 and data["columns"][0]["label"] == "Order"
    assert sap.calls[-1] == ("report_dict", ("wip",), {})


def test_the_edw_list_is_the_overlays_dict():
    sap = _FakeSap()
    status, data = _call(_app(sap), "GET", "/api/sap/edw")
    assert status == 200 and data["plant"] == "1006" and data["view_prefix"] == "TV_"


# ---- the writes ------------------------------------------------------------------------------

def test_the_sql_route_posts_the_whole_body_as_the_spec():
    sap = _FakeSap()
    app = _app(sap)
    spec = {"tables": ["AFRU", "AFVC"], "columns": [{"table": "AFRU", "field": "BUDAT"}],
            "filters": [], "recipes": ["system_status"], "root": "AFRU", "limit": 50}
    status, data = _post(app, "/api/sap/sql", spec)
    assert status == 200 and data["sql"] == "SELECT 1"
    assert sap.calls[-1] == ("sql_dict", (spec,), {})


def test_a_sql_body_that_is_not_an_object_is_refused_not_crashed():
    sap = _FakeSap()
    app = _app(sap)
    status, data = _post(app, "/api/sap/sql", ["AFRU"])
    assert status == 400 and "error" in data
    status, data = _call(app, "POST", "/api/sap/sql", b"not json", "application/json")
    assert status == 400 and "error" in data
    assert not sap.calls                                     # the service never saw either


def test_recording_the_edw_passes_action_table_and_text_from_the_body():
    sap = _FakeSap()
    app = _app(sap)
    status, data = _post(app, "/api/sap/edw", {"action": "record_columns", "table": "AFRU",
                                               "text": "MANDT\nRUECK\nRMZHL"})
    assert status == 200 and data == {"ok": True, "message": "Recorded AFRU."}
    assert sap.calls[-1] == ("edw_record", ("record_columns",),
                             {"table": "AFRU", "text": "MANDT\nRUECK\nRMZHL"})
    # a partial body still reaches the service with empty strings, never None
    _post(app, "/api/sap/edw", {"action": "missing", "table": "MDTB"})
    assert sap.calls[-1] == ("edw_record", ("missing",), {"table": "MDTB", "text": ""})
    status, data = _post(app, "/api/sap/edw", "just a string")
    assert status == 400 and "error" in data


# ---- the guards ------------------------------------------------------------------------------

def test_every_sap_route_is_503_when_the_faculty_is_not_wired():
    app = _app(None)
    for method, url in (("GET", "/api/sap/status"), ("GET", "/api/sap/search?q=afru"),
                        ("GET", "/api/sap/table/AFRU"), ("GET", "/api/sap/join?tables=AFRU,AFVC"),
                        ("GET", "/api/sap/report/wip"), ("GET", "/api/sap/edw")):
        status, data = _call(app, method, url)
        assert (status, data) == (503, {"error": "SAP faculty unavailable"}), url
    for url, payload in (("/api/sap/sql", {"tables": ["AFRU"]}),
                         ("/api/sap/edw", {"action": "missing", "table": "MDTB"})):
        status, data = _post(app, url, payload)
        assert (status, data) == (503, {"error": "SAP faculty unavailable"}), url


def test_a_container_without_the_attribute_at_all_is_also_503():
    container = SimpleNamespace(settings=_Settings(web_token="tok-test"),
                                paths=SimpleNamespace(builds="does-not-exist"))
    app = build_app(container, SimpleNamespace(), EventHub(), None)
    status, data = _call(app, "GET", "/api/sap/status")
    assert (status, data) == (503, {"error": "SAP faculty unavailable"})


def test_a_service_that_raises_answers_a_json_error_not_a_traceback():
    sap = _FakeSap()
    sap.boom = {"table_dict", "edw_record"}
    app = _app(sap)
    status, data = _call(app, "GET", "/api/sap/table/AFRU")
    assert status == 500 and data == {"error": "table_dict fell over"}
    status, data = _post(app, "/api/sap/edw", {"action": "forget", "table": "AFRU"})
    assert status == 500 and data == {"error": "edw_record fell over"}


def test_the_sap_routes_sit_behind_the_token_guard():
    sap = _FakeSap()
    status, data = _call(_app(sap), "GET", "/api/sap/status", token="")
    assert status == 401 and data == {"error": "unauthorized"} and not sap.calls
