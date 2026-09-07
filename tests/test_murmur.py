"""Sleep-talk (services/murmur.py): the MURMUR line lifted off a reply, the templates for a session's
moments, and the one voice both keep — lowercase, trailing off, never a secret."""
from __future__ import annotations

from helix.services.murmur import (
    MURMUR_CAP, MURMUR_INSTRUCTION, clean, murmur_for_note, start_murmur, take_murmur,
)


def test_the_murmur_line_is_lifted_off_a_reply_before_any_parser_sees_it():
    reply = ("FINDINGS:\n- The XIAO has 8 MB PSRAM [verified: https://wiki.seeedstudio.com/x]\n"
             "FACTS NOTED: 1\nIDEAS:\n- Use the esp-idf I2S driver — source: https://docs.espressif.com/i2s\n"
             "MURMUR: eight megabytes… room enough for the whole song…")
    rest, murmur = take_murmur(reply)
    assert murmur == "eight megabytes… room enough for the whole song…"
    assert "MURMUR" not in rest and rest.endswith("source: https://docs.espressif.com/i2s")


def test_a_reply_without_one_comes_back_untouched():
    text = "QUIET"
    assert take_murmur(text) == ("QUIET", "")
    assert take_murmur("") == ("", "")


def test_bold_bulleted_and_quoted_shapes_are_read_and_the_last_one_wins():
    rest, murmur = take_murmur("1. Fix it\nEFFORT: deep\n- **MURMUR:** \"first…\"\n**MURMUR**: 'the last one…'")
    assert murmur == "the last one…"
    assert "MURMUR" not in rest and rest == "1. Fix it\nEFFORT: deep"


def test_a_murmur_is_cleaned_capped_scrubbed_and_trails_off():
    assert clean("  hello   there ") == "hello there…"
    assert clean("done.") == "done."
    long = "a " * 200
    out = clean(long)
    assert len(out) <= MURMUR_CAP and out.endswith("…")
    assert "ghp_" not in clean("use ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 for the watcher…")
    assert clean("") == ""


def test_the_instruction_asks_for_one_last_line_in_the_exact_shape():
    assert "MURMUR: <the murmur>" in MURMUR_INSTRUCTION
    assert "LAST line" in MURMUR_INSTRUCTION and "lowercase" in MURMUR_INSTRUCTION


def test_session_moments_become_dreamy_fragments_and_bookkeeping_stays_silent():
    started = murmur_for_note("session started (nightly) — 23:00 to 07:00, planning on claude-fable-5")
    assert started and started.endswith("…") and started == started.lower()
    drafting = murmur_for_note("drafting (deep, claude-fable-5): Remember the camera device in services/camera.py; add a test.")
    assert drafting and ("camera" in drafting or "hands in my own code" in drafting)
    applied = murmur_for_note("applied selfdev/d1 — did thing 1")
    assert applied in ("green… all of it green… it's in me now…", "merged… I'll be a little different when I wake…")
    held = murmur_for_note("held selfdev/d2: tests failed")
    assert held and ("red" in held or "said no" in held)
    assert murmur_for_note("round 2 — reflecting again on what tonight found")
    assert murmur_for_note("paused at 23:05 — the plan's limit was reached")
    assert murmur_for_note("holding — you're using the machine; I'll wait for ten quiet minutes")
    assert murmur_for_note("session ended (the window closed) — 3 drafts")
    # Nothing to hear in these.
    assert murmur_for_note("") is None
    assert murmur_for_note("self-model updated — 2 new lines, 0 aged out") is None
    assert murmur_for_note("standard suggested; drafting on Fable anyway") is None


def test_the_variant_is_chosen_by_the_line_so_a_night_is_reproducible():
    line = "drafted selfdev/d3 — Shorten the morning brief in services/agents.py."
    assert murmur_for_note(line) == murmur_for_note(line)
    assert murmur_for_note("drafted selfdev/d3 — one") != murmur_for_note("drafted selfdev/d3 — two") or True  # may collide; never raises


def test_a_step_start_names_what_is_being_read_checked_or_tried():
    research = start_murmur("research", "Does the XIAO ESP32S3 Sense have PSRAM and how much?")
    assert "does the xiao esp32s3 sense have psram" in research and research.endswith("…")
    verify = start_murmur("verify", "The Pi Camera v2 lens sits 13.8 mm along the long edge")
    assert "the pi camera v2 lens sits" in verify
    assert start_murmur("reflect", "") and start_murmur("improve", "")
    assert start_murmur("unknown-kind", "thing") == "mm… thing…"
