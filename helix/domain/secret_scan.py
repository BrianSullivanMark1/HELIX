"""The secrets scan - does a branch carry anything that must never be in a repo?

Pure: text in, findings out. The patterns are the ones that bite: cloud keys, private keys,
GitHub / Slack / Stripe tokens, service-account JSON, connection strings with passwords, and the
files that should never be committed at all (.env, *.pem, service-account key files). Findings
carry the line but NEVER the secret itself - the value is masked before it leaves this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

SKIP_DIRS = {"node_modules", "dist", "build", ".git", "__pycache__", ".venv", "venv", ".next", "coverage", ".firebase"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".mp3", ".mp4", ".m4a", ".wav", ".zip", ".gz", ".tar", ".pdf", ".xlsx", ".xls", ".docx", ".pptx",
            ".lock", ".min.js", ".map", ".pyc", ".exe", ".dll", ".so", ".bin", ".glb", ".gltf", ".step", ".stl"}
MAX_BYTES = 400 * 1024

# (name, severity, pattern) - severity: "high" must not ship, "medium" look at it
PATTERNS: tuple[tuple[str, str, re.Pattern], ...] = (
    ("Private key block", "high", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("Google service-account key", "high", re.compile(r'"type"\s*:\s*"service_account"')),
    ("Google API key", "high", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("AWS access key", "high", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", "high", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("Slack token", "high", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b")),
    ("Stripe key", "high", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{20,}\b")),
    ("Anthropic API key", "high", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("OpenAI API key", "high", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("SendGrid key", "high", re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b")),
    ("Twilio key", "medium", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("Connection string with a password", "high", re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)://[^\s:/]+:[^\s@/]{4,}@", re.I)),
    ("Password in code", "medium", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")),
    ("Firebase config with a key", "medium", re.compile(r"\bapiKey\s*:\s*['\"]AIza[0-9A-Za-z_\-]{35}['\"]")),
    ("JWT", "medium", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
)
# files that should not be in a repo at all, whatever they contain
FORBIDDEN_NAMES: tuple[tuple[str, str, re.Pattern], ...] = (
    ("An .env file is committed", "high", re.compile(r"(?:^|/)\.env(?:\.[A-Za-z0-9_-]+)?$")),
    ("A private key file is committed", "high", re.compile(r"\.(?:pem|key|p12|pfx)$", re.I)),
    ("A service-account key file is committed", "high", re.compile(r"(?:service[-_]?account|serviceaccount|firebase-adminsdk)[^/]*\.json$", re.I)),
    ("Firebase debug log is committed", "medium", re.compile(r"(?:^|/)firebase-debug\.log$")),
)
ALLOW_LINE = re.compile(r"(?i)\b(?:example|placeholder|your[-_ ]?key|xxxx|changeme|dummy|<[^>]+>)\b")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int              # 0 for a whole-file finding
    kind: str
    severity: str          # high | medium
    excerpt: str           # masked - never the secret

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "kind": self.kind, "severity": self.severity, "excerpt": self.excerpt}


@dataclass
class Report:
    files_scanned: int = 0
    files_skipped: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        high = sum(1 for f in self.findings if f.severity == "high")
        return {"files_scanned": self.files_scanned, "files_skipped": self.files_skipped,
                "high": high, "medium": len(self.findings) - high, "clean": self.clean,
                "findings": [f.as_dict() for f in self.findings]}


def mask(text: str, m: re.Match) -> str:
    """The matched value is replaced by its first 4 characters and stars, so a report can be
    pasted anywhere without becoming the leak it describes."""
    s = m.group(0)
    keep = s[:4] if len(s) > 8 else ""
    return text[:m.start()] + keep + "*" * min(24, max(4, len(s) - len(keep))) + text[m.end():]


def scannable(path: str) -> bool:
    p = PurePosixPath(path)
    if any(part in SKIP_DIRS for part in p.parts[:-1]):
        return False
    name = p.name.lower()
    return not any(name.endswith(ext) for ext in SKIP_EXT)


def scan_name(path: str) -> list[Finding]:
    out = []
    for kind, sev, rx in FORBIDDEN_NAMES:
        if rx.search(path):
            out.append(Finding(path=path, line=0, kind=kind, severity=sev, excerpt=PurePosixPath(path).name))
    return out


def scan_text(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 4000:
            line = line[:4000]
        if ALLOW_LINE.search(line):
            continue
        for kind, sev, rx in PATTERNS:
            m = rx.search(line)
            if m:
                shown = mask(line, m).strip()
                out.append(Finding(path=path, line=i, kind=kind, severity=sev, excerpt=shown[:160]))
                break   # one finding per line is enough to act on
    return out


def scan_files(files) -> Report:
    """`files` yields (path, bytes-or-None). None means 'unreadable or too big' - counted, skipped."""
    rep = Report()
    for path, data in files:
        rep.findings.extend(scan_name(path))
        if not scannable(path):
            rep.files_skipped += 1
            continue
        if data is None or len(data) > MAX_BYTES:
            rep.files_skipped += 1
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            rep.files_skipped += 1
            continue
        if "\x00" in text[:2000]:
            rep.files_skipped += 1
            continue
        rep.files_scanned += 1
        rep.findings.extend(scan_text(path, text))
    rep.findings.sort(key=lambda f: (0 if f.severity == "high" else 1, f.path, f.line))
    return rep
