"""WebVoice — the VoiceController ported off Qt, for the web shell's backend process.

A faithful port of helix/ui/voice.py's state machine onto sounddevice + threads: the SAME pure
grammar (services/voicegrammar — one brainstem, both shells), the same listen gate ("the mic is live
only while HELIX is genuinely idle", with the camera session's narrow exception), the same playback
gate over the machine's own audio (adapters/mediasense), the same identity gate and enrollment flow,
the same sleep/wake semantics, and the same TTS streaming with generation-counter preemption.

Differences from the Qt original, all mechanical:
  - QAudioSource → one sounddevice input stream, opened while voice is enabled; a listener thread
    drains it (VAD, level/bands events) and PTT borrows the same stream instead of re-opening the
    device.
  - Qt signals → plain callbacks the ShellSession wires (state/level/bands/muted/identity lines out;
    recognized commands and stop requests in).
  - QtWorker/QTimer → daemon threads and threading.Timer. Handlers run on the transcription thread
    under one RLock — the web shell has no UI thread to marshal to.

Barge-in remains deliberately disabled exactly as in V3 Qt: the gate deafens the mic while HELIX
thinks, speaks, or works; deliberate stops are UI gestures.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from helix.adapters.mediasense import MediaSense
from helix.domain.brain import is_directly_addressed
from helix.logging_setup import get_logger
from helix.ports.speech import SpeechIn, SpeechOut
from helix.ports.stores import SettingsStore
from helix.services import voiceid
from helix.services.voicegrammar import (
    _RECENT_SPEAKER_S,
    _SLEEP_CONFIRM,
    _WAKE_CONFIRM,
    PTT_MAX_MS,
    SESSION_IDLE_MS,
    STT_PREWARM_ERROR_SETTING,
    VOICE_SETTING,
    WAKE_WORD_SETTING,
    VadSegmenter,
    _pcm_bands,
    _pcm_rms,
    _wants_wake,
    _write_wav16,
    build_wake_re,
    camera_command,
    is_dismissal,
    is_sleep,
    is_stop,
    is_wake,
    speakable,
    split_sentences,
    split_wake,
)

_LOG = get_logger("webvoice")

_SAMPLE_RATE = 16000
_BLOCK = 1600  # 100 ms of 16 kHz mono int16 per callback
_LEVEL_EVERY_S = 1 / 15  # UI level/bands cadence


def _sounddevice():
    try:
        import sounddevice  # noqa: PLC0415 — optional, probed at call time

        return sounddevice
    except Exception:  # noqa: BLE001
        return None


class WebVoice:
    """The web shell's ears and voice. Public surface mirrors ui.voice.VoiceController."""

    def __init__(
        self,
        settings: SettingsStore,
        stt: SpeechIn,
        tts: SpeechOut,
        voice_id=None,
        reflexes=None,
        *,
        on_state: Callable[[str], None] = lambda s: None,
        on_level: Callable[[float], None] = lambda v: None,
        on_bands: Callable[[list], None] = lambda b: None,
        on_muted: Callable[[bool], None] = lambda m: None,
        on_identity: Callable[[str, str], None] = lambda h, r: None,
        on_recognized: Callable[[str], None] = lambda t: None,
        on_stop: Callable[[], None] = lambda: None,
    ) -> None:
        self._settings = settings
        self._stt = stt
        self._tts = tts
        self._voice_id = voice_id
        self._reflexes = reflexes
        self._flow = voiceid.EnrollmentFlow(voice_id) if voice_id is not None else None
        self._media = MediaSense()
        self.on_state, self.on_level, self.on_bands = on_state, on_level, on_bands
        self.on_muted, self.on_identity = on_muted, on_identity
        self.on_recognized, self.on_stop = on_recognized, on_stop

        self._lock = threading.RLock()
        self._state = "idle"
        self._muted = False
        self._working = False
        self._session = False
        self._session_speaker: str | None = None
        self._session_timer: threading.Timer | None = None
        self._camera_session: tuple | None = None
        self._camera_stt_busy = False
        self._barge_busy = False
        self._narrating = False
        self._speaking_text = ""
        self._speak_gen = 0
        self._pending_emb = None
        self._last_speaker: str | None = None
        self._last_speaker_ts = 0.0
        self.current_speaker: str | None = None
        self._wake_re = build_wake_re(settings.get(WAKE_WORD_SETTING) or "")

        self._stream = None
        self._stream_lock = threading.Lock()
        self._vad = VadSegmenter(_SAMPLE_RATE)
        self._listening = False   # the gate's output: is VAD processing live?
        self._ptt = False
        self._ptt_buf = bytearray()
        self._ptt_timer: threading.Timer | None = None
        self._last_level_ts = 0.0
        self._closed = False

        if self.enabled():
            self._start_stream()

    # ----- capability gates (same predicates, same order, as the Qt shell) -----
    def mic_available(self) -> bool:
        sd = _sounddevice()
        if sd is None:
            return False
        try:
            return sd.query_devices(kind="input") is not None
        except Exception:  # noqa: BLE001
            return False

    def can_listen(self) -> bool:
        return self.mic_available() and self._stt.available() and self._stt.ready()

    def supported(self) -> bool:
        return self.mic_available() and self._stt.available()

    def prewarm_error(self) -> str:
        return str(self._settings.get(STT_PREWARM_ERROR_SETTING) or "")

    def restart_required(self) -> bool:
        return self.supported() and not self._stt.ready() and not self.prewarm_error()

    def enabled(self) -> bool:
        return bool(self._settings.get(VOICE_SETTING, False))

    def set_enabled(self, on: bool) -> None:
        self._settings.set(VOICE_SETTING, bool(on))
        if on:
            self._start_stream()
        else:
            self._stop_stream()
            self._end_session()
            self._muted = False
        self._set_state("idle")

    def reload_audio_input(self) -> None:
        """Re-read the wake word and re-open the stream — a settings save needs no restart."""
        self._wake_re = build_wake_re(self._settings.get(WAKE_WORD_SETTING) or "")
        if self.enabled():
            self._stop_stream()
            self._start_stream()

    # ----- the audio stream -----
    def _start_stream(self) -> None:
        sd = _sounddevice()
        if sd is None or self._closed:
            return
        with self._stream_lock:
            if self._stream is not None:
                return
            try:
                self._stream = sd.RawInputStream(
                    samplerate=_SAMPLE_RATE, channels=1, dtype="int16", blocksize=_BLOCK,
                    callback=self._on_audio,
                )
                self._stream.start()
            except Exception:  # noqa: BLE001 — no mic just means a text app
                _LOG.warning("could not open the microphone", exc_info=True)
                self._stream = None
        self._apply_listen_gate()

    def _stop_stream(self) -> None:
        with self._stream_lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _on_audio(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        chunk = bytes(indata)
        try:
            self._media.tick()
        except Exception:  # noqa: BLE001
            pass
        now = time.monotonic()
        if now - self._last_level_ts >= _LEVEL_EVERY_S:
            self._last_level_ts = now
            rms = _pcm_rms(chunk)
            try:
                self.on_level(min(1.0, rms / 8000.0))
                self.on_bands(_pcm_bands(chunk))
            except Exception:  # noqa: BLE001
                pass
        if self._ptt:
            self._ptt_buf += chunk
            return
        if not self._listening:
            self._vad.reset()
            return
        utter = self._vad.push(chunk)
        if utter is not None:
            threading.Thread(
                target=self._on_utterance, args=(utter,), daemon=True, name="helix-voice-utter"
            ).start()

    # ----- the listen gate (THE one rule — verbatim from the Qt shell) -----
    def _apply_listen_gate(self) -> None:
        camera = (
            self._camera_session is not None
            and not self._muted
            and self._state != "speaking"
        )
        self._listening = bool(
            self.enabled() and not self._working and (self._state == "idle" or camera)
        )

    def set_working(self, on: bool) -> None:
        on = bool(on)
        if on == self._working:
            return
        self._working = on
        self._apply_listen_gate()

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state
            self._apply_listen_gate()
        try:
            self.on_state(state)
        except Exception:  # noqa: BLE001
            pass

    # ----- session -----
    def _start_session(self) -> None:
        self._session = True
        if self._session_timer is not None:
            self._session_timer.cancel()
        self._session_timer = threading.Timer(SESSION_IDLE_MS / 1000.0, self._end_session)
        self._session_timer.daemon = True
        self._session_timer.start()

    def _end_session(self) -> None:
        self._session = False
        if self._session_timer is not None:
            self._session_timer.cancel()
            self._session_timer = None
        self._session_speaker = None
        if self._flow is not None and self._flow.active:
            self._flow.cancel()

    # ----- camera session -----
    def set_camera_session(self, on_capture, on_cancel) -> None:
        self._camera_session = (on_capture, on_cancel)
        self._camera_stt_busy = False
        self._apply_listen_gate()

    def clear_camera_session(self) -> None:
        self._camera_session = None
        self._apply_listen_gate()

    def camera_ears_live(self) -> bool:
        return self.enabled() and self.can_listen() and not self._muted and not self._working

    # ----- sleep / wake -----
    def is_muted(self) -> bool:
        return self._muted

    def _is_sleep_reflex(self, command: str) -> bool:
        if is_sleep(command):
            return True
        return self._reflexes is not None and self._reflexes.matches(command, "sleep")

    def learn_sleep(self, command: str) -> None:
        if self._reflexes is None or not (command or "").strip() or is_sleep(command):
            return
        try:
            self._reflexes.learn(command, "sleep")
        except Exception:  # noqa: BLE001
            pass

    def set_muted(self, on: bool, announce: bool = True) -> None:
        on = bool(on)
        if on and not self.can_listen():
            return
        if on == self._muted:
            return
        self._muted = on
        if on:
            self._narrating = False
        try:
            self.on_muted(on)
        except Exception:  # noqa: BLE001
            pass
        if announce and self.enabled() and self._tts.available():
            self.speak(_SLEEP_CONFIRM if on else _WAKE_CONFIRM)
        else:
            if on:
                self._hush_tts()
            self._set_state("idle")

    def toggle_muted(self) -> None:
        self.set_muted(not self._muted)

    def _on_muted_text(self, text: str, media: bool = False) -> None:
        self._barge_busy = False
        t = (text or "").strip()
        if t and _wants_wake(t, self._wake_re):
            if media and not self._known_voice():
                matched, after = split_wake(t, self._wake_re)
                if not (matched and after and is_directly_addressed(t, self._wake_re)):
                    self._set_state("idle")
                    return
            self.set_muted(False)
            return
        if t and is_stop(t):
            self._hush()
            self.on_stop()
        self._set_state("idle")

    # ----- utterance routing (branch order preserved exactly) -----
    def _on_utterance(self, pcm: bytes) -> None:
        if self._muted:
            if self._barge_busy:
                return
            path = self._pcm_to_wav(pcm)
            if path is None:
                return
            media = self._media_playing()
            self._barge_busy = True
            self._transcribe(path, lambda text: self._on_muted_text(text, media), pcm)
            return
        if (
            self._camera_session is not None
            and self._state != "speaking"
            and not self._working
        ):
            if self._camera_stt_busy:
                return
            path = self._pcm_to_wav(pcm)
            if path is None:
                return
            media = self._media_playing()
            session = self._camera_session
            self._camera_stt_busy = True
            self._transcribe(path, lambda text: self._on_camera_text(text, media, session), pcm)
            return
        if self._working or self._state != "idle":
            return
        path = self._pcm_to_wav(pcm)
        if path is None:
            return
        media = self._media_playing()
        self._set_state("transcribing")
        self._transcribe(path, lambda text: self._on_wake_text(text, media), pcm)

    def _hush_tts(self) -> None:
        try:
            self._tts.stop()
        except Exception:  # noqa: BLE001
            pass

    def _hush(self) -> None:
        self._hush_tts()
        self._narrating = False

    def _media_playing(self) -> bool:
        try:
            return self._media.playing()
        except Exception:  # noqa: BLE001
            return False

    def _known_voice(self) -> bool:
        svc, emb = self._voice_id, self._pending_emb
        if svc is None or emb is None:
            return False
        try:
            return bool(svc.identify(emb).name)
        except Exception:  # noqa: BLE001
            return False

    # ----- identity -----
    def _take_emb(self):
        emb, self._pending_emb = self._pending_emb, None
        return emb

    def _say_identity(self, heard: str, reply: str) -> None:
        try:
            self.on_identity(heard, reply)
        except Exception:  # noqa: BLE001
            pass
        self.speak(reply)

    def _after_flow(self, heard: str, reply: str | None) -> None:
        flow = self._flow
        if reply:
            self._start_session()
            self._say_identity(heard, reply)
        else:
            self._set_state("idle")
        name = getattr(flow, "last_registered", None)
        if name and not flow.active:
            flow.last_registered = None
            self.current_speaker = name
            self._session_speaker = name
            if self._voice_id is not None:
                self._voice_id.distill_notes(name, getattr(flow, "last_answers", []))

    def _flow_intercept(self, text: str, media: bool = False) -> bool:
        flow = self._flow
        if flow is None or not flow.active or not text:
            return False
        if media and len(text.split()) > 8 and not is_directly_addressed(text, self._wake_re):
            return False
        if is_stop(text) or is_dismissal(text):
            flow.cancel()
            self.interrupt()
            return True
        matched, after = split_wake(text, self._wake_re)
        reply = flow.handle(after if matched and after else text, self._pending_emb)
        if reply is None:
            return False
        self._pending_emb = None
        self._after_flow(text, reply)
        return True

    def _gate(self, command: str) -> bool:
        svc, flow = self._voice_id, self._flow
        self.current_speaker = None
        if svc is None:
            return True
        emb = self._take_emb()
        has = svc.has_profiles()
        res = svc.identify(emb)
        name = res.name
        intro = voiceid.introduction_name(command)
        if voiceid.wants_recalibration(command):
            if name:
                self._say_identity(command, flow.start(name, emb, recal=True))
            elif not has:
                self._say_identity(command, flow.ask_name())
            else:
                self._say_identity(command, flow.offer())
            self._start_session()
            return False
        if intro:
            if name and res.confident and intro.lower() == name.lower():
                self._session_speaker = name
                self._start_session()
                self._say_identity(command, f"I know your voice, {name}. What can I do for you?")
                return False
            if name and res.confident:
                self._say_identity(
                    command,
                    f"You sound like {name} to me. If someone new wants to register, "
                    "they should say it themselves — I am, then their name.",
                )
                return False
            self._start_session()
            self._say_identity(command, flow.start(intro, emb))
            return False
        if voiceid.wants_registration(command):
            if name:
                self._say_identity(
                    command, f"Your voice is already registered, {name}. "
                    "Say: recalibrate my voice, to refresh it.")
            else:
                self._start_session()
                self._say_identity(command, flow.ask_name())
            return False
        if not has:
            return True
        if name:
            self.current_speaker = name
            self._session_speaker = name
            self._remember_speaker(name)
            if res.confident:
                threading.Thread(
                    target=svc.add_passive, args=(name, emb), daemon=True, name="helix-voiceid"
                ).start()
            return True
        if res.no_evidence:
            if self._session and self._session_speaker:
                self.current_speaker = self._session_speaker
                return True
            if self._recent_speaker():
                self.current_speaker = self._recent_speaker()
                return True
        if bool(self._settings.get("trust_household_voice", False)):
            who = self._session_speaker or self._recent_speaker()
            if who is None:
                names = svc.names()
                who = names[0] if len(names) == 1 else None
            self.current_speaker = who
            return True
        self._say_identity(command, flow.offer())
        return False

    def _remember_speaker(self, name: str) -> None:
        self._last_speaker = name
        self._last_speaker_ts = time.monotonic()

    def _recent_speaker(self) -> str | None:
        if self._last_speaker and (time.monotonic() - self._last_speaker_ts) <= _RECENT_SPEAKER_S:
            return self._last_speaker
        return None

    def _ack(self) -> str:
        who = self._session_speaker or self.current_speaker or self._recent_speaker()
        return f"Yes, {who}?" if who else "Yes?"

    def _on_camera_text(self, text: str, media: bool, session=None) -> None:
        self._camera_stt_busy = False
        cam = self._camera_session
        if cam is None or self._muted:
            return
        if session is not None and session is not cam:
            return
        text = (text or "").strip()
        if not text:
            return
        if media and not self._known_voice() and not is_directly_addressed(text, self._wake_re):
            return
        action = camera_command(text, self._wake_re)
        if action is None:
            return
        on_capture, on_cancel = cam
        try:
            (on_capture if action == "capture" else on_cancel)()
        except Exception:  # noqa: BLE001
            _LOG.exception("camera voice command failed")

    def _on_wake_text(self, text: str, media: bool = False) -> None:
        text = (text or "").strip()
        if self._muted:
            self._on_muted_text(text, media)
            return
        if self._flow_intercept(text, media):
            return
        matched, after = split_wake(text, self._wake_re)
        if media and not self._known_voice():
            dismiss = self._session and is_dismissal(text) and matched
            if (matched and not after and not dismiss) or not (
                dismiss or is_directly_addressed(text, self._wake_re)
            ):
                self._set_state("idle")
                return
        if self._session and is_dismissal(text):
            who = self._session_speaker or self.current_speaker
            self._end_session()
            self.speak(f"Until next time, {who}." if who else "Until next time.")
            return
        if matched:
            command = after.strip()
        elif self._session and text:
            command = text
        else:
            self._set_state("idle")
            return
        if self._is_sleep_reflex(command):
            self.set_muted(True)
            return
        if is_wake(command):
            self._set_state("idle")
            return
        if is_stop(command):
            self.interrupt()
            self.on_stop()
            return
        if not command:
            self._start_session()
            self.speak(self._ack())
            return
        if not self._gate(command):
            return
        self._start_session()
        self._set_state("thinking")
        try:
            self.on_recognized(command)
        except Exception:  # noqa: BLE001
            _LOG.exception("recognized-command handler failed")
            self._set_state("idle")

    # ----- push-to-talk -----
    def ptt_start(self) -> bool:
        if self._state != "idle" or not self.can_listen():
            return False
        self._start_stream()
        if self._stream is None:
            return False
        self._ptt_buf = bytearray()
        self._ptt = True
        self._ptt_timer = threading.Timer(PTT_MAX_MS / 1000.0, self._ptt_watchdog)
        self._ptt_timer.daemon = True
        self._ptt_timer.start()
        self._set_state("listening")
        return True

    def _ptt_watchdog(self) -> None:
        if self._ptt:
            self.ptt_stop()

    def ptt_stop(self) -> None:
        if not self._ptt:
            return
        self._ptt = False
        if self._ptt_timer is not None:
            self._ptt_timer.cancel()
            self._ptt_timer = None
        data = bytes(self._ptt_buf)
        self._ptt_buf = bytearray()
        if not self.enabled():
            self._stop_stream()  # PTT opened the stream just for this hold
        if not data:
            self._set_state("idle")
            return
        self._set_state("transcribing")
        path = self._pcm_to_wav(data)
        if path is None:
            self._set_state("idle")
            return
        self._transcribe(path, self._on_ptt_text, data)

    def _on_ptt_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self._set_state("idle")
            return
        if self._camera_session is not None and not self._muted:
            self._set_state("idle")
            self._on_camera_text(text, media=False, session=self._camera_session)
            return
        if self._muted:
            self._on_muted_text(text)
            return
        if self._flow_intercept(text):
            return
        if self._is_sleep_reflex(text):
            self.set_muted(True)
            return
        if is_wake(text):
            self._set_state("idle")
            return
        if not self._gate(text):
            return
        self._start_session()
        self._set_state("thinking")
        try:
            self.on_recognized(text)
        except Exception:  # noqa: BLE001
            _LOG.exception("recognized-command handler failed")
            self._set_state("idle")

    # ----- speaking -----
    def begin_turn(self) -> None:
        self._set_state("thinking")

    def speak(self, text: str) -> None:
        self._narrating = False
        self._hush_tts()
        text = speakable(text)
        if not text or not self._tts.available():
            self._set_state("idle")
            return
        self._speaking_text = text
        self._set_state("speaking")
        self._speak_gen += 1
        gen = self._speak_gen
        chunks = split_sentences(text)
        threading.Thread(
            target=self._speak_thread, args=(chunks, gen), daemon=True, name="helix-voice-speak"
        ).start()

    def _speak_thread(self, chunks: list[str], gen: int) -> None:
        try:
            if gen != self._speak_gen:
                return
            speak_chunks = getattr(self._tts, "speak_chunks", None)
            if callable(speak_chunks):
                speak_chunks(chunks)
            else:
                for chunk in chunks:
                    if gen != self._speak_gen:
                        return
                    self._tts.speak(chunk)
        except Exception:  # noqa: BLE001
            _LOG.warning("speak failed", exc_info=True)
        finally:
            self._speak_done(gen)

    def _speak_done(self, gen: int) -> None:
        if gen == self._speak_gen and self._state == "speaking":
            self._set_state("idle")

    def narrate(self, text: str, force: bool = False) -> None:
        if self._narrating or not self.enabled():
            return
        if self._muted and not force:
            return
        if self._state == "speaking":
            return
        text = speakable(text)
        if not text or not self._tts.available():
            return
        self._speaking_text = text
        self._narrating = True

        def go() -> None:
            try:
                self._tts.speak(text, allow_fallback=False)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._narrating = False

        threading.Thread(target=go, daemon=True, name="helix-voice-narrate").start()

    def murmur(self, text: str) -> None:
        """Sleep-talk, whispered (services/murmur.py, READ_ME/DREAM_MIND.md §14): only while the
        voice is idle — never over a reply, a progress note or a listening mic — and never while
        muted (the user asked for quiet). The TTS's own murmur() is the same voice quieter and
        slower; a backend without one stays silent. Runs on its own thread; the echo shield
        compares overheard speech to it exactly as it does a narration note."""
        if not self.enabled() or self._muted or self._narrating or self._state != "idle":
            return
        fn = getattr(self._tts, "murmur", None)
        text = speakable(text)
        if not text or not callable(fn) or not self._tts.available():
            return
        self._speaking_text = text
        self._narrating = True

        def go() -> None:
            try:
                fn(text)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._narrating = False

        threading.Thread(target=go, daemon=True, name="helix-voice-murmur").start()

    def idle(self) -> None:
        self._set_state("idle")

    def is_active(self) -> bool:
        return self._state != "idle"

    def state(self) -> str:
        return self._state

    def interrupt(self) -> None:
        self._hush_tts()
        self._narrating = False
        self._set_state("idle")

    # ----- transcription plumbing -----
    @staticmethod
    def _pcm_to_wav(pcm: bytes) -> str | None:
        try:
            handle, path = tempfile.mkstemp(suffix=".wav", prefix="helix_wake_")
            os.close(handle)
            _write_wav16(pcm, path)
            return path
        except Exception:  # noqa: BLE001
            return None

    def _transcribe(self, path: str, on_text: Callable[[str], None], pcm: bytes | None = None) -> None:
        def work() -> None:
            emb = None
            text = ""
            try:
                if pcm is not None and self._voice_id is not None:
                    try:
                        emb = voiceid.embed_pcm(pcm)
                    except Exception:  # noqa: BLE001
                        emb = None
                try:
                    text = self._stt.transcribe(Path(path))
                except Exception:  # noqa: BLE001
                    text = ""
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            # (text, voice-print) travel together; the print lands immediately before its own
            # handler under the lock, so overlapping transcriptions can't cross-pair speakers.
            with self._lock:
                self._pending_emb = emb
                try:
                    on_text(text)
                except Exception:  # noqa: BLE001
                    _LOG.exception("utterance handler failed")

        threading.Thread(target=work, daemon=True, name="helix-voice-stt").start()

    def shutdown(self) -> None:
        self._closed = True
        self._stop_stream()
        self._hush_tts()
        if self._session_timer is not None:
            self._session_timer.cancel()
        if self._ptt_timer is not None:
            self._ptt_timer.cancel()
