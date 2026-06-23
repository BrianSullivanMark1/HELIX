"""FallbackCoder — prefer the Claude Code CLI; fall back to the API coder.

Selection is lazy (per run_task), because availability changes at runtime: a fresh install has no key,
so neither coder is available until the user adds one in Settings.
"""
from __future__ import annotations

from pathlib import Path

from helix.logging_setup import get_logger
from helix.ports.coder import CoderAgent, CoderResult, ProgressFn

_LOG = get_logger("coder")


class FallbackCoder:
    name = "auto"

    def __init__(self, primary: CoderAgent, fallback: CoderAgent) -> None:
        self._primary = primary
        self._fallback = fallback

    def available(self) -> bool:
        return self._primary.available() or self._fallback.available()

    def run_task(
        self, repo_dir: Path, prompt: str, *, on_progress: ProgressFn | None = None
    ) -> CoderResult:
        if self._primary.available():
            _LOG.info("building with %s", self._primary.name)
            result = self._primary.run_task(repo_dir, prompt, on_progress=on_progress)
            if result.ok:
                return result
            _LOG.warning("%s failed (%s); trying %s", self._primary.name, result.error, self._fallback.name)
            if self._fallback.available():
                if on_progress:
                    on_progress("Switching to the built-in builder…")
                return self._fallback.run_task(repo_dir, prompt, on_progress=on_progress)
            return result
        if self._fallback.available():
            _LOG.info("building with %s", self._fallback.name)
            return self._fallback.run_task(repo_dir, prompt, on_progress=on_progress)
        return CoderResult(
            ok=False, summary="", error="No coder available — add your Claude API key in Settings."
        )
