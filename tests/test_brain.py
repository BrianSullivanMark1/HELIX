"""The biomimetic brain: thalamic addressing gate, learned reflexes, growth-model resolver."""
from __future__ import annotations

from helix.adapters.model_select import PREFERRED_GROWTH_MODEL, best_growth_model
from helix.domain.brain import Layer, is_addressed, is_wake_utterance
from helix.services.reflexes import ReflexService
from helix.ui.voice import build_wake_re, is_wake


def _wake():
    return build_wake_re("HELIX")


# ----- thalamic gate: addressed TO me vs. spoken ABOUT me -----

def test_name_leading_the_utterance_is_addressed():
    rx = _wake()
    for phrase in ("HELIX", "HELIX?", "hey HELIX", "HELIX build me a timer",
                   "good morning HELIX, how you doing", "morning HELIX", "HELIX are you there"):
        assert is_addressed(phrase, rx), phrase


def test_name_mentioned_mid_sentence_is_not_addressed():
    rx = _wake()
    for phrase in ("the wake word is HELIX", "you wake it by saying HELIX",
                   "so anyway HELIX can build apps and stuff", "I was telling Dave about HELIX yesterday",
                   "the command to wake HELIX is just its name"):
        assert not is_addressed(phrase, rx), phrase


def test_salutation_anywhere_with_the_name_counts_as_addressed():
    rx = _wake()
    assert is_addressed("well hello there HELIX", rx)          # greeting + name = spoken to it
    assert not is_addressed("we should tell them HELIX exists", rx)  # no opener, name late = about it


def test_carrier_words_never_hide_an_address():
    # Carriers — the wake grammar's own hey/ok/okay plus the imperative "please" (with STT's beloved
    # commas) — may precede the name and it still LEADS: these address exactly like "HELIX, ...".
    rx = _wake()
    for phrase in ("please HELIX, turn the music down", "okay, HELIX, do it",
                   "okay helix open the report", "hey helix turn the lights off"):
        assert is_addressed(phrase, rx), phrase


def test_narrative_leads_are_mentions_not_addresses():
    # so/well/oh are NARRATIVE leads, not carriers: "so HELIX built..." reports ABOUT it, and lyrics
    # love "oh <name> ..." — none of these may wake a sleeping HELIX or pass the playback gate.
    rx = _wake()
    for phrase in ("so anyway HELIX can build apps and stuff",
                   "so HELIX built me an app yesterday",
                   "well HELIX is pretty weird sometimes",
                   "oh HELIX take me home tonight",
                   "people say please HELIX far too often these days"):
        assert not is_addressed(phrase, rx), phrase


def test_directly_addressed_withholds_the_short_fragment_benefit():
    from helix.domain.brain import is_directly_addressed

    rx = _wake()
    # A short fragment containing the name is_addressed (benefit of the doubt for a live mic)...
    assert is_addressed("my helix baby", rx)
    # ...but is NOT directly addressed — the strict test the playback gate uses.
    assert not is_directly_addressed("my helix baby", rx)
    assert is_directly_addressed("helix stop", rx)
    assert is_directly_addressed("hey helix, what time is it", rx)
    assert is_directly_addressed("good morning HELIX, how you doing", rx)


def test_custom_wake_words_survive_the_carrier_and_token_rules():
    from helix.domain.brain import is_directly_addressed
    from helix.ui.voice import build_wake_re

    # A wake word that IS a carrier word ("Okay") is never stripped as one — it stays the lead.
    rx = build_wake_re("Okay")
    assert is_addressed("okay turn the lights on now", rx)
    # A MULTI-word wake word matches on joined text, so it can lead an address too.
    rx2 = build_wake_re("red queen")
    assert is_directly_addressed("red queen stop the music", rx2)
    assert not is_directly_addressed("I told them about red queen yesterday ok", rx2)


def test_wake_utterance_composes_phrase_and_gate():
    rx = _wake()
    assert is_wake_utterance("wake up", rx, is_wake)                     # explicit phrase
    assert is_wake_utterance("good morning HELIX how are you", rx, is_wake)  # addressed
    assert not is_wake_utterance("the wake word is HELIX", rx, is_wake)  # mention → stays asleep


def test_custom_wake_word_respected_by_the_gate():
    rx = build_wake_re("Nimbus")
    assert is_addressed("hey Nimbus", rx)
    assert not is_addressed("the assistant is named Nimbus", rx)


def test_layers_are_the_five_named_stages():
    assert {ly.value for ly in Layer} == {"brainstem", "thalamus", "limbic", "cortex", "growth"}


# ----- learned reflexes: consolidation + developmental pruning -----

class _Store:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def test_a_learned_phrase_becomes_an_instant_reflex():
    r = ReflexService(_Store())
    assert not r.matches("power down")                  # not known yet
    assert r.learn("power down")                        # the cortex consolidates it
    assert r.matches("power down")                      # now it's a fast reflex
    assert r.matches("okay power down now please")      # fillers (okay/now/please) normalized away


def test_reflexes_ignore_non_command_shaped_phrases():
    r = ReflexService(_Store())
    assert not r.learn("")                              # empty
    long = "please could you consider going into a resting state sometime this evening thanks"
    assert not r.learn(long)                            # too many words — never a crisp command


def test_reflexes_prune_the_least_recently_fired_over_the_cap():
    from helix.services import reflexes as reflex_mod

    r = ReflexService(_Store())
    for i in range(reflex_mod._MAX_PER_KIND + 5):
        r.learn(f"sleep code {i}")
    kept = r.phrases()
    assert len(kept) == reflex_mod._MAX_PER_KIND        # capped, not unbounded
    assert "sleep code 0" not in kept                   # the oldest were pruned
    assert f"sleep code {reflex_mod._MAX_PER_KIND + 4}" in kept  # the newest survive


# ----- growth model: Fable 5 floor, auto-upscale to a stronger family/version -----

def test_growth_model_defaults_to_the_fable_floor():
    assert best_growth_model([]) == PREFERRED_GROWTH_MODEL
    # A weaker line never displaces the Fable floor.
    assert best_growth_model(["claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-8"]) \
        == PREFERRED_GROWTH_MODEL


def test_growth_model_auto_upscales_to_a_future_fable_6():
    ids = ["claude-fable-5", "claude-fable-6", "claude-opus-5-0", "claude-sonnet-5"]
    assert best_growth_model(ids) == "claude-fable-6"


def test_growth_model_upscales_within_a_family_by_version():
    assert best_growth_model(["claude-fable-5", "claude-fable-5-1"]) == "claude-fable-5-1"


def test_growth_model_ignores_unknown_families():
    # An unrelated new id must never capture growth reasoning.
    assert best_growth_model(["claude-experimental-9", "claude-fable-5"]) == "claude-fable-5"


def test_a_stronger_top_tier_family_wins():
    # Mythos ranks above Fable — if it ever appears in the list, growth upscales to it.
    assert best_growth_model(["claude-fable-5", "claude-mythos-5"]) == "claude-mythos-5"


def test_malformed_ids_never_crash_or_downgrade():
    # Junk ids rank 0 and never displace the pinned floor.
    assert best_growth_model(["", "claude", "gpt-4", "claude-fable", "not-a-model"]) \
        == PREFERRED_GROWTH_MODEL


def test_resolver_never_blocks_and_floors_without_a_key():
    from helix.adapters.model_select import GrowthModelResolver

    r = GrowthModelResolver(lambda: "")   # no key → the floor, no network
    assert r.resolve() == PREFERRED_GROWTH_MODEL
    assert r.resolve() == PREFERRED_GROWTH_MODEL  # idempotent, still instant


def test_work_model_tiers_floor_at_opus_and_top_at_fable():
    from helix.adapters.model_select import WORK_FLOOR_MODEL, GrowthModelResolver

    r = GrowthModelResolver(lambda: "")  # no key → resolve() is the Fable floor
    assert r.work_model(deep=False) == WORK_FLOOR_MODEL == "claude-opus-4-8"
    assert r.work_model(deep=True) == PREFERRED_GROWTH_MODEL == "claude-fable-5"
    # The standard (floor) tier is never stronger than the deep tier.
    assert best_growth_model([r.work_model(False), r.work_model(True)]) == r.work_model(True)


def test_resolver_uses_a_no_redirect_opener():
    # The authenticated model-list request must refuse redirects (no x-api-key leak), matching call_api.
    from helix.adapters import model_select

    assert any(
        isinstance(h, model_select._NoRedirect) for h in model_select._OPENER.handlers
    )
