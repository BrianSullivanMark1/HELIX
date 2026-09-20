"""The secrets scan: finds the things that bite, masks every value, skips what is not text or
should not be read, and flags forbidden file names even when their content is fine."""
from __future__ import annotations

from helix.domain import secret_scan as sc


def test_keys_are_found_and_masked():
    key = "AIza" + "SyD-Q9r7Tz2" + "0123456789abcdefghijklmn"   # 4 + 35 characters, the real shape
    assert len(key) == 39
    text = f'API = "{key}"\nnothing here\n'
    f = sc.scan_text("web/src/config.ts", text)
    assert len(f) == 1 and f[0].kind == "Google API key" and f[0].line == 1 and f[0].severity == "high"
    assert "AIza" in f[0].excerpt and "Q9r7Tz2" not in f[0].excerpt and "****" in f[0].excerpt


def test_one_finding_per_line_and_placeholders_are_ignored():
    text = 'password = "hunter2hunter2"  # AKIAABCDEFGHIJKLMNOP\napiKey: "AIzaSy-placeholder-xxxxxxxxxxxxxxxxxx"\n'
    f = sc.scan_text("a.py", text)
    assert len(f) == 1 and f[0].line == 1


def test_private_key_blocks_and_service_accounts():
    f = sc.scan_text("k.txt", "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n")
    assert f and f[0].kind == "Private key block"
    f = sc.scan_text("sa.json", '{\n  "type": "service_account",\n  "project_id": "x"\n}')
    assert f and f[0].kind == "Google service-account key"


def test_forbidden_names_are_flagged_and_binaries_skipped():
    files = [
        ("backend/.env", b"FLASK_ENV=dev\n"),
        ("keys/firebase-adminsdk-abc.json", b"{}"),
        ("node_modules/x/index.js", b'token = "ghp_' + b"a" * 40 + b'"'),
        ("logo.png", b"\x89PNG"),
        ("app.py", b"x = 1\n"),
        ("big.txt", None),
    ]
    rep = sc.scan_files(files)
    kinds = {f.kind for f in rep.findings}
    assert "An .env file is committed" in kinds and "A service-account key file is committed" in kinds
    assert not any(f.path.startswith("node_modules") for f in rep.findings)   # skipped, never read
    assert rep.files_scanned == 3 and rep.files_skipped == 3                  # .env, sa.json, app.py read; node_modules, png, big skipped
    d = rep.as_dict()
    assert d["high"] == 2 and d["clean"] is False


def test_clean_repo_is_clean():
    rep = sc.scan_files([("app.py", b"print('hi')\n"), ("README.md", b"# HELIX\n")])
    assert rep.clean and rep.as_dict()["findings"] == []


def test_connection_strings_and_github_tokens():
    f = sc.scan_text("settings.py", 'DB = "postgres://oats:Sup3rS3cret@10.0.0.4/mrp"\n')
    assert f and f[0].kind == "Connection string with a password" and "Sup3rS3cret" not in f[0].excerpt
    f = sc.scan_text("deploy.ps1", "$t = 'github_pat_" + "A" * 70 + "'\n")
    assert f and f[0].kind == "GitHub token"
