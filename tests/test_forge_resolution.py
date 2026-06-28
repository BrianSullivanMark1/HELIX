"""ForgeService build-name resolution — the 'update my garden' → the right build behaviour.

Exercises _resolve_prior directly (it only needs name/kind data, not the coder/repo/bus), locking the
fuzzy fallback that fixes 'HELIX can't find the model to edit': a paraphrase resolves to the build the
user means, but only when it's unambiguous and the same kind.
"""
from __future__ import annotations

from pathlib import Path

from helix.domain.models import App, BuildKind
from helix.services.forge import ForgeService


def _forge() -> ForgeService:
    # _resolve_prior touches none of the wired collaborators, so None placeholders are fine here.
    return ForgeService(builds=None, coder=None, bus=None, repo=None, app_root=Path("."))


def _app(name: str, slug: str, kind: BuildKind) -> App:
    return App(slug=slug, name=name, request="", build_kind=kind)


def test_exact_slug_and_name_still_win():
    f = _forge()
    existing = [_app("Garden Walkthrough", "garden-walkthrough", BuildKind.MODEL)]
    assert f._resolve_prior("garden-walkthrough", "garden-walkthrough", BuildKind.MODEL, existing).slug == (
        "garden-walkthrough"
    )
    assert f._resolve_prior("Garden Walkthrough", "x", BuildKind.MODEL, existing).slug == "garden-walkthrough"


def test_fuzzy_resolves_a_paraphrase_to_the_only_same_kind_build():
    f = _forge()
    existing = [
        _app("Garden Walkthrough", "garden-walkthrough", BuildKind.MODEL),
        _app("Tip Calculator", "tip-calculator", BuildKind.APP),
    ]
    # "garden", "my garden model" — all the obvious ways a user refers to it — find the one model.
    for said in ("garden", "my garden model", "the garden"):
        prior = f._resolve_prior(said, "garden", BuildKind.MODEL, existing)
        assert prior is not None and prior.slug == "garden-walkthrough", said


def test_fuzzy_is_kind_scoped():
    f = _forge()
    existing = [_app("Garden Walkthrough", "garden-walkthrough", BuildKind.MODEL)]
    # Asking to build an APP called "garden" must NOT hijack the MODEL — it makes a new app.
    assert f._resolve_prior("garden", "garden", BuildKind.APP, existing) is None


def test_fuzzy_ambiguity_makes_a_new_build():
    f = _forge()
    existing = [
        _app("Garden Walkthrough", "garden-walkthrough", BuildKind.MODEL),
        _app("Garden Pond", "garden-pond", BuildKind.MODEL),
    ]
    # Two models contain "garden" — don't guess; fall through to a new build (None).
    assert f._resolve_prior("garden", "garden", BuildKind.MODEL, existing) is None
