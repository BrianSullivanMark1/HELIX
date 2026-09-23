"""THE VOICE: Gemini-TTS through the person's gcloud token - styled in plain words, cached, falling
back to edge-tts and never leaving a line unspoken. No network, no gcloud: runner and http are doubles."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from helix.adapters import google_tts as g
from helix.adapters.gcp_secret_manager import Ran
from helix.api import say as say_mod

TOK = lambda argv, t: Ran(0, "tok\n", "")   # noqa: E731


def _http(log, *, status=200, audio=b"MP3BYTES", body=None):
    def http(method, url, token, payload, timeout):
        log.append((method, url, token, payload))
        if status != 200:
            return status, body or '{"error":{"message":"nope"}}'
        return 200, json.dumps({"audioContent": base64.b64encode(audio).decode()})
    return http


def test_the_request_carries_voice_style_and_model_and_repeats_are_cached():
    log = []
    t = g.GoogleTts("windy-celerity-392822", runner=TOK, http=_http(log))
    out = t.synthesize("HELIX online.", "Charon", g.style_prompt("butler"))
    assert out == b"MP3BYTES"
    m, url, tok, body = log[0]
    assert m == "POST" and url.endswith("/v1/text:synthesize") and tok == "tok"
    assert body["voice"] == {"languageCode": "en-US", "name": "Charon", "model_name": g.DEFAULT_MODEL}
    assert body["input"]["text"] == "HELIX online." and "British" in body["input"]["prompt"]
    assert body["audioConfig"] == {"audioEncoding": "MP3"}
    assert t.synthesize("HELIX online.", "Charon", g.style_prompt("butler")) == b"MP3BYTES" and len(log) == 1   # cached
    t.synthesize("HELIX online.", "Charon", g.style_prompt("dubliner"))
    assert len(log) == 2 and "Irish" in log[1][3]["input"]["prompt"]                                              # a new style is a new line
    t.synthesize("Hello", "Kore", "")
    assert "prompt" not in log[2][3]["input"]                                                                     # Plain sends no prompt
    t.synthesize("Hello", "NotAVoice", "", whisper=True)
    assert log[3][3]["voice"]["name"] == g.DEFAULT_VOICE and "Whisper" in log[3][3]["input"]["prompt"]


def test_refusals_are_sentences_that_name_the_fix():
    t = g.GoogleTts("p", runner=TOK, http=_http([], status=403, body='{"error":{"message":"Cloud Text-to-Speech API has not been used in project p before or it is disabled"}}'))
    try:
        t.synthesize("x", "Charon", ""); assert False
    except g.TtsError as e:
        assert "gcloud services enable texttospeech" in str(e)
    t = g.GoogleTts("p", runner=TOK, http=_http([], status=403), identity=lambda: "kate@mark1online.com")
    try:
        t.synthesize("x", "Charon", ""); assert False
    except g.TtsError as e:
        assert str(e).startswith("kate@mark1online.com may not") and "Service Usage Consumer" in str(e)
    t = g.GoogleTts("p", runner=lambda a, s: Ran(1, "", ""), http=_http([]))
    try:
        t.synthesize("x", "Charon", ""); assert False
    except g.TtsError as e:
        assert "signed in" in str(e)


class _Fallback:
    def __init__(self):
        self.spoke = []
        self.stopped = 0

    def available(self):
        return True

    def speak(self, text, allow_fallback=True):
        self.spoke.append(text)

    def murmur(self, text):
        self.spoke.append("(murmur) " + text)

    def stop(self):
        self.stopped += 1


def _out(tts, engine="google", played=None):
    fb = _Fallback()
    o = g.GoogleSpeechOut(tts, engine=lambda: engine, voice=lambda: "Charon", style=lambda: g.style_prompt("butler"), fallback=fb)
    played = played if played is not None else []
    o._player = SimpleNamespace(play=lambda path, dur: (played.append(path), True)[1], stop=lambda: None)
    return o, fb, played


def test_google_speaks_when_chosen_edge_when_not_and_when_google_refuses(monkeypatch):
    monkeypatch.setattr(g, "mp3_duration_ms", lambda p: 1200)
    t = g.GoogleTts("p", runner=TOK, http=_http([]))
    o, fb, played = _out(t)
    o.speak("Good evening.")
    assert len(played) == 1 and fb.spoke == []
    o.speak_chunks(["One.", "Two."])
    assert len(played) == 2 and fb.spoke == []                     # one request for the whole reply
    o.murmur("night")
    assert len(played) == 3
    o2, fb2, played2 = _out(t, engine="edge")
    o2.speak("Good evening.")
    assert played2 == [] and fb2.spoke == ["Good evening."]          # the setting says edge: straight through
    bad = g.GoogleTts("p", runner=TOK, http=_http([], status=500))
    o3, fb3, played3 = _out(bad)
    o3.speak("Still here.")
    assert played3 == [] and fb3.spoke == ["Still here."] and "answered 500" in (bad.last_error or "")
    o3.speak("A progress note", allow_fallback=False)
    assert fb3.spoke == ["Still here."]                              # one voice for narration: skipped, not swapped
    o3.stop()
    assert fb3.stopped == 1


class _Shell:
    def __init__(self):
        self.pushed = []
        self.voice = None

    def push(self, ev):
        self.pushed.append(ev)


def test_say_speaks_through_google_first_and_previews_do_not_bubble():
    tts = g.GoogleTts("p", runner=TOK, http=_http([], audio=b"GOOGLE"))
    lines = say_mod.Lines()
    settings = {"tts_engine": "google", "tts_google_voice": "Kore", "tts_style": "dubliner"}
    shell = _Shell()
    r = say_mod.say(shell, "Hello there.", settings=SimpleNamespace(get=settings.get), synth=lambda t, v, r: (b"EDGE", []), lines=lines, google=tts)
    assert r["ok"] and r["engine"] == "google" and r["words"] == [] and lines.get(r["id"]) == b"GOOGLE"
    assert shell.pushed and shell.pushed[0]["t"] == "msg"
    # Google refuses -> edge speaks, and the answer says so
    bad = g.GoogleTts("p", runner=TOK, http=_http([], status=500))
    r = say_mod.say(_Shell(), "Hello.", settings=SimpleNamespace(get=settings.get), synth=lambda t, v, r: (b"EDGE", []), lines=lines, google=bad)
    assert r["engine"] == "edge" and r["fell_back"].startswith("google:")
    # a preview: this voice and style for one line, no bubble
    log = []
    tts2 = g.GoogleTts("p", runner=TOK, http=_http(log, audio=b"P"))
    sh = _Shell()
    r = say_mod.say(sh, "Try me.", settings=SimpleNamespace(get=settings.get), synth=None, lines=lines, google=tts2,
                    over={"voice": "Algenib", "style": "custom", "style_custom": "Speak like a pirate.", "preview": True}, bubble=False)
    assert r["ok"] and sh.pushed == [] and log[0][3]["voice"]["name"] == "Algenib" and log[0][3]["input"]["prompt"] == "Speak like a pirate."
    # the setting says edge: Google is never asked
    r = say_mod.say(_Shell(), "Hi.", settings=SimpleNamespace(get={"tts_engine": "edge"}.get), synth=lambda t, v, r: (b"EDGE", []), lines=lines, google=tts2)
    assert r["engine"] == "edge" and len(log) == 1


def test_the_catalog_has_thirty_voices_and_the_styles_speak_plainly():
    cat = g.catalog()
    assert len(cat["voices"]) == 30 and sum(1 for v in cat["voices"] if v["pick"]) == 8
    assert {v["gender"] for v in cat["voices"]} == {"male", "female"}
    keys = [s["key"] for s in cat["styles"]]
    assert keys[:2] == ["butler", "dubliner"] and "custom" in keys and "plain" in keys
    assert g.style_prompt("custom", "  Speak slowly.  ") == "Speak slowly." and g.style_prompt("nope") == g.style_prompt("butler")
