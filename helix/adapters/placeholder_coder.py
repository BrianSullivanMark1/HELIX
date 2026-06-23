"""PlaceholderCoder — a temporary CoderAgent so the app runs end-to-end before the real one lands.

It writes a small branded starter app instead of truly building from the request. Phase 6 replaces it
with the Claude Code CLI adapter and an Anthropic-API fallback. Wired in app/container.py.
"""
from __future__ import annotations

from pathlib import Path

from helix.ports.coder import CoderResult, ProgressFn

_STARTER = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HELIX app</title>
<style>
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:#080b0f; color:#e2edf1; font-family:system-ui,sans-serif; }
  .card { text-align:center; padding:40px 48px; border:1px solid #1b2730; border-radius:16px;
          background:#0d141b; box-shadow:0 0 60px rgba(63,224,224,.08); }
  h1 { color:#3fe0e0; font-weight:600; margin:0 0 8px; }
  p { color:#7a8a93; margin:0; }
</style></head>
<body><div class="card">
  <h1>◉ Built by HELIX</h1>
  <p>The real coding agent is coming online next — this is a starter shell.</p>
</div></body></html>
"""


class PlaceholderCoder:
    name = "placeholder"

    def available(self) -> bool:
        return True

    def run_task(
        self, repo_dir: Path, prompt: str, *, on_progress: ProgressFn | None = None
    ) -> CoderResult:
        if on_progress:
            on_progress("Writing a starter app…")
        (repo_dir / "index.html").write_text(_STARTER, encoding="utf-8")
        return CoderResult(
            ok=True,
            summary="created a starter app (placeholder coder — real agent lands in phase 6)",
            changed_paths=("index.html",),
        )
