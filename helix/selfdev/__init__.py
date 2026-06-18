"""HELIX's self-improvement loop — "HELIX builds HELIX" (§selfdev).

HELIX improves its own code with Opus 4.8 via the Claude Code CLI, gated by human approval. The loop:

    trigger (a crash in data/helix.log, or a spoken/typed request to Xpert)
      -> branch off the deployed code (gitops)
      -> Opus 4.8 edits the files on that branch (coder)
      -> smoke-check (imports cleanly + app boots)        [engine, later]
      -> email Brian the diff + ask; or voice-approve      [approval, later]
      -> merge to main + restart in a safe window          [engine, later]

This package is built bottom-up. `coder.py` + `gitops.py` are the foundation: they let HELIX make a
reviewable, reversible code change on a `selfdev/*` branch. main is NEVER touched without explicit
human approval, and any work branch can be deleted to discard the change entirely.

Self-improvement is the engineering-on-itself face of the Enterprise pillar.
"""
from __future__ import annotations
