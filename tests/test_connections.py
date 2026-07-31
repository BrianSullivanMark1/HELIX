"""ConnectionsService — the Forge's API-key capability: reading a build's declared keys, storing/injecting
values, the read-only call_api guard rails, and the 'a backend build runs' classification fix."""
from __future__ import annotations

import json
from pathlib import Path

from helix.domain.connections import KNOWN_SERVICES, looks_like_mispaste, service_for_url
from helix.domain.models import AppKind
from helix.services.builds import BuildService
from helix.services.connections import CONNECTABLE, ConnectionsService, resolve_connectable


class _Builds:
    def __init__(self, root: Path) -> None:
        self._root = root

    def workspace(self, slug: str) -> Path:
        return self._root / slug


class _Secrets:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def _svc(tmp_path: Path) -> ConnectionsService:
    return ConnectionsService(_Builds(tmp_path), _Secrets())


def _declare(tmp_path: Path, slug: str, entries: list) -> None:
    ws = tmp_path / slug
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "connections.json").write_text(json.dumps(entries), encoding="utf-8")


def test_declared_parses_and_ignores_junk(tmp_path):
    s = _svc(tmp_path)
    _declare(tmp_path, "foo", [
        {"key": "SLACK_TOKEN", "label": "Slack token", "hint": "xoxp-"},
        {"nope": 1},            # no key — ignored
        {"key": "   "},         # blank key — ignored
        {"key": "GITHUB_TOKEN"},  # label/hint default
    ])
    conns = s.declared("foo")
    assert [c.key for c in conns] == ["SLACK_TOKEN", "GITHUB_TOKEN"]
    assert conns[0].label == "Slack token" and conns[0].hint == "xoxp-"
    assert conns[1].label == "GITHUB_TOKEN"  # falls back to the key name
    assert s.declared("missing") == []       # no file → nothing declared


def test_values_env_and_missing(tmp_path):
    s = _svc(tmp_path)
    _declare(tmp_path, "foo", [{"key": "SLACK_TOKEN", "label": "Slack"},
                               {"key": "GITHUB_TOKEN", "label": "GitHub"}])
    assert s.needs_connection("foo") is True
    assert [c.key for c in s.missing("foo")] == ["SLACK_TOKEN", "GITHUB_TOKEN"]
    s.set_value("SLACK_TOKEN", "  xoxp-abc  ")
    assert s.value("SLACK_TOKEN") == "xoxp-abc"          # trimmed
    assert s.env_for("foo") == {"SLACK_TOKEN": "xoxp-abc"}  # only the SET key is injected
    assert [c.key for c in s.missing("foo")] == ["GITHUB_TOKEN"]


def test_call_api_refuses_non_https_unknown_and_unconnected(tmp_path):
    s = _svc(tmp_path)
    assert "https" in s.call_api("http://slack.com/api/auth.test").lower()
    assert "connectable service" in s.call_api("https://evil.example.com/x")
    # known host but no token yet
    out = s.call_api("https://slack.com/api/auth.test")
    assert "isn't connected" in out and "Slack" in out


def test_call_api_attaches_bearer_and_never_returns_token(tmp_path, monkeypatch):
    s = _svc(tmp_path)
    s.set_value("SLACK_TOKEN", "tok-secret-123")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b'{"ok":true,"team":"Acme"}'

    import helix.services.connections as mod

    def fake_open(req, timeout=0):
        captured["auth"] = req.get_header("Authorization")
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(mod._OPENER, "open", fake_open)
    out = s.call_api("https://slack.com/api/auth.test")
    assert '{"ok":true,"team":"Acme"}' in out           # body returned (now inside an untrusted-data fence)
    assert "untrusted external CONTENT" in out          # fenced + labelled as data, never instructions
    assert captured["auth"] == "Bearer tok-secret-123"  # token attached server-side
    assert "tok-secret-123" not in out                  # never leaked back to the model


def test_call_api_never_follows_redirects():
    # The critical guard: a redirect must never be auto-followed (it would re-send the token to an
    # unvalidated host). The opener's handler returns None for every 3xx.
    import helix.services.connections as mod
    assert mod._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil.example/x") is None


def test_service_for_url_matching():
    assert service_for_url("https://slack.com/api/conversations.list").id == "slack"
    assert service_for_url("https://api.github.com/user/repos").id == "github"
    assert service_for_url("https://files.slack.com/x").id == "slack"  # subdomain
    assert service_for_url("https://paper-api.alpaca.markets/v2/account").id == "alpaca"
    assert service_for_url("https://data.alpaca.markets/v2/stocks/AAPL/trades/latest").id == "alpaca"
    # SAM.gov spans the site search API (sam.gov, the one the website itself runs on) AND the
    # documented public host (api.sam.gov) — both must resolve so either path works.
    assert service_for_url("https://sam.gov/api/prod/sgs/v1/search/?index=opp&q=x").id == "sam"
    assert service_for_url("https://api.sam.gov/opportunities/v2/search").id == "sam"
    assert service_for_url("https://evil.example.com/slack.com").id != "slack" \
        if service_for_url("https://evil.example.com/slack.com") else True  # path can't spoof host
    assert service_for_url("https://evil.example.com") is None
    assert {s.env for s in KNOWN_SERVICES} == {
        "SLACK_TOKEN", "GITHUB_TOKEN", "ALPACA_API_KEY", "SAM_API_KEY",
    }


def test_call_api_alpaca_needs_both_keys_and_sends_both_headers(tmp_path, monkeypatch):
    s = _svc(tmp_path)
    # One credential alone is not "connected" — Alpaca needs the key id AND the secret.
    s.set_value("ALPACA_API_KEY", "AK-id-123")
    out = s.call_api("https://paper-api.alpaca.markets/v2/account")
    assert "isn't connected" in out and "Alpaca" in out

    s.set_value("ALPACA_SECRET_KEY", "sec-secret-999")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b'{"status":"ACTIVE","cash":"1000"}'

    import helix.services.connections as mod

    def fake_open(req, timeout=0):
        # urllib capitalizes header keys; Alpaca (like all HTTP) treats them case-insensitively.
        captured["id"] = req.get_header("Apca-api-key-id")
        captured["secret"] = req.get_header("Apca-api-secret-key")
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(mod._OPENER, "open", fake_open)
    out = s.call_api("https://paper-api.alpaca.markets/v2/account")
    assert '{"status":"ACTIVE","cash":"1000"}' in out  # body returned inside the untrusted-data fence
    assert captured["id"] == "AK-id-123"           # both credentials attached as headers, server-side
    assert captured["secret"] == "sec-secret-999"
    assert captured["auth"] is None                # Alpaca uses its own headers, not a Bearer token
    assert "sec-secret-999" not in out             # the secret is never leaked back to the model


def test_call_api_sam_attaches_query_key_and_accept_header(tmp_path, monkeypatch):
    # SAM.gov auths via ?api_key= (not a header): the stored key is attached SERVER-SIDE, any
    # same-named param the model guessed is stripped so the real key always wins, and the service's
    # static Accept header rides along (the sgs endpoint 406s without application/hal+json).
    s = _svc(tmp_path)
    s.set_value("SAM_API_KEY", "sam-key-abc123")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b'{"_embedded":{"results":[{"title":"RFQ"}]}}'

    import helix.services.connections as mod

    def fake_open(req, timeout=0):
        captured["url"] = req.full_url
        captured["accept"] = req.get_header("Accept")
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(mod._OPENER, "open", fake_open)
    out = s.call_api("https://sam.gov/api/prod/sgs/v1/search/?index=opp&q=software&api_key=GUESSED")
    assert "api_key=sam-key-abc123" in captured["url"]      # stored key attached as a query param
    assert "GUESSED" not in captured["url"]                 # the model's guess is stripped
    assert "application/hal+json" in captured["accept"]     # per-service Accept applied
    assert captured["auth"] is None                         # query-param service: no Bearer header
    assert '"title":"RFQ"' in out and "untrusted external CONTENT" in out
    assert "sam-key-abc123" not in out                      # never leaked back to the model


def test_call_api_sam_http_error_carries_the_recipe(tmp_path, monkeypatch):
    # api.sam.gov's gateway answers wrong paths with an EMPTY 404 — the error the model gets back
    # must carry the known-good request shapes so it can retry correctly in the same turn.
    import urllib.error

    import helix.services.connections as mod

    s = _svc(tmp_path)
    s.set_value("SAM_API_KEY", "sam-key-abc123")

    def fake_open(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(mod._OPENER, "open", fake_open)
    out = s.call_api("https://api.sam.gov/opportunities/v9/search")
    assert "HTTP 404" in out
    assert "Known-good SAM.gov request shapes" in out
    assert "sgs/v1/search" in out and "opportunities/v2/search" in out
    assert "sam-key-abc123" not in out  # the key never appears, even in an error echoing the URL

    # a service with no recipe (Slack) still returns the plain error, no hint appended
    s.set_value("SLACK_TOKEN", "tok-1")
    out2 = s.call_api("https://slack.com/api/nope")
    assert "HTTP 404" in out2 and "Known-good" not in out2


def test_managed_keys_are_reused_from_helix_not_re_prompted(tmp_path):
    # A build that needs AI declares ANTHROPIC_API_KEY but the user never pastes it — HELIX provides its
    # existing Claude key (which lives in Settings, not the secrets store). This is the OpenAI->Claude fix.
    managed = {"ANTHROPIC_API_KEY": lambda: "sk-ant-managed", "TRIPO_API_KEY": lambda: ""}
    s = ConnectionsService(_Builds(tmp_path), _Secrets(), managed=managed)
    _declare(tmp_path, "aiapp", [
        {"key": "ANTHROPIC_API_KEY", "label": "Claude"},
        {"key": "SLACK_TOKEN", "label": "Slack"},
    ])
    assert s.value("ANTHROPIC_API_KEY") == "sk-ant-managed"  # resolved from the managed getter
    assert s.is_managed("ANTHROPIC_API_KEY") and not s.is_managed("SLACK_TOKEN")
    # env_for injects the managed key automatically; the unset Slack one is simply absent
    assert s.env_for("aiapp") == {"ANTHROPIC_API_KEY": "sk-ant-managed"}
    # the build is NOT reported as missing the Claude key — only the genuinely-unset Slack token
    assert [c.key for c in s.missing("aiapp")] == ["SLACK_TOKEN"]


def test_a_pasted_secret_wins_over_a_managed_key(tmp_path):
    sec = _Secrets()
    sec.set("ANTHROPIC_API_KEY", "from-secrets")
    s = ConnectionsService(_Builds(tmp_path), sec, managed={"ANTHROPIC_API_KEY": lambda: "from-settings"})
    assert s.value("ANTHROPIC_API_KEY") == "from-secrets"  # an explicit paste still takes precedence


def test_connectable_covers_every_known_service_plus_the_engine_keys():
    # The just-in-time connect panel and the Settings manager both iterate CONNECTABLE — it must
    # span call_api's allow-list AND the engine keys, with a valid store on every entry.
    assert set(CONNECTABLE) >= {s.id for s in KNOWN_SERVICES} | {"tripo", "blockade", "voyage"}
    for label, store, fields in CONNECTABLE.values():
        assert label and store in ("secrets", "settings") and fields
        for key, field_label, _hint in fields:
            assert key and field_label
    # a KNOWN_SERVICES entry keeps its env-var keys in the secrets store
    assert CONNECTABLE["slack"][1] == "secrets"
    assert [k for k, _l, _h in CONNECTABLE["slack"][2]] == ["SLACK_TOKEN"]
    # an engine key ALSO lives in the secrets store (guard-safe), under its env-var name
    assert CONNECTABLE["tripo"][1] == "secrets"
    assert [k for k, _l, _h in CONNECTABLE["tripo"][2]] == ["TRIPO_API_KEY"]


def test_resolve_connectable_maps_spoken_names_ids_and_aliases():
    assert resolve_connectable("Slack") == "slack"
    assert resolve_connectable("  GITHUB ") == "github"
    assert resolve_connectable("sam.gov") == "sam"      # alias
    assert resolve_connectable("skybox") == "blockade"  # alias
    assert resolve_connectable("tripo") == "tripo"
    assert resolve_connectable("SAM.") == "sam"         # label prefix
    assert resolve_connectable("teapot") is None
    assert resolve_connectable("") is None


def test_looks_like_mispaste_flags_urls_only():
    # The mis-paste that actually happened: SAM.gov's endpoint URL pasted into the key field, which
    # then rode every request as a bogus token. URLs are flagged; unusual-but-real keys are not.
    assert looks_like_mispaste("https://api.sam.gov/opportunities/v2/search")
    assert looks_like_mispaste("  HTTP://example.com  ")
    assert looks_like_mispaste("www.sam.gov")
    assert looks_like_mispaste("ftp://files.example.com")
    # the scheme-less rendering of the same mis-paste (docs and error messages print it this way)
    assert looks_like_mispaste("api.sam.gov/opportunities/v2/search")
    assert looks_like_mispaste("sam.gov/api/prod/sgs/v1/search/")
    assert looks_like_mispaste("tsk_abc123") == ""
    assert looks_like_mispaste("abcd efgh ijkl mnop") == ""  # Google app passwords contain spaces
    assert looks_like_mispaste("xoxp-1234-5678") == ""
    assert looks_like_mispaste("eyjhbg.eyjzdw.sflkxw") == ""  # dotted-but-slashless (a JWT) is fine
    assert looks_like_mispaste("") == ""


def test_detect_entry_prefers_a_backend_main_py(tmp_path):
    # The Slack-dashboard fix: a build with index.html + main.py runs the backend, not the landing page.
    ws = tmp_path / "app"
    ws.mkdir()
    (ws / "index.html").write_text("<h1>start here</h1>", encoding="utf-8")
    (ws / "main.py").write_text("print('server')", encoding="utf-8")
    kind, entry = BuildService(tmp_path, None, None)._detect_entry(ws)
    assert kind == AppKind.PYTHON and entry == "main.py"
    # a pure web app (no main.py) still resolves to its page
    ws2 = tmp_path / "web"
    ws2.mkdir()
    (ws2 / "index.html").write_text("<h1>app</h1>", encoding="utf-8")
    kind2, entry2 = BuildService(tmp_path, None, None)._detect_entry(ws2)
    assert kind2 == AppKind.HTML and entry2 == "index.html"
