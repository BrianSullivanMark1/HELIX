from __future__ import annotations

import asyncio
import os
import tempfile

# Free, natural, neural voices via edge-tts (Microsoft Edge "Read Aloud"). en-GB-RyanNeural
# is a British male voice that suits the J.A.R.V.I.S. tone.
DEFAULT_VOICE = "en-GB-RyanNeural"
DEFAULT_RATE = "+50%"  # ~1.5x speaking speed for the Xpert briefing

# A curated set of free edge-tts English neural voices the user can pick from in Settings.
# (voice_id, human label). Pure data — no network call needed to populate the picker. Ryan
# (the J.A.R.V.I.S. default) leads. These are all real edge-tts voice ids.
VOICE_CHOICES = (
    ("en-GB-RyanNeural", "Ryan — British male (default)"),
    ("en-GB-ThomasNeural", "Thomas — British male"),
    ("en-GB-SoniaNeural", "Sonia — British female"),
    ("en-US-GuyNeural", "Guy — US male"),
    ("en-US-ChristopherNeural", "Christopher — US male"),
    ("en-US-AriaNeural", "Aria — US female"),
    ("en-US-JennyNeural", "Jenny — US female"),
    ("en-AU-WilliamNeural", "William — Australian male"),
    ("en-AU-NatashaNeural", "Natasha — Australian female"),
    ("en-IE-ConnorNeural", "Connor — Irish male"),
    ("en-CA-LiamNeural", "Liam — Canadian male"),
)


def synthesize_speech(text: str, voice: str = DEFAULT_VOICE, rate: str = DEFAULT_RATE) -> str:
    """Synthesize `text` to an MP3 with edge-tts and return the file path.

    `rate` is an edge-tts percentage (e.g. "+50%" ≈ 1.5x). Runs on a worker thread (it makes a
    network call). `edge_tts` is imported lazily so the app still loads and falls back to the
    built-in OS voice if the package is missing.
    """
    import edge_tts

    handle, path = tempfile.mkstemp(suffix=".mp3", prefix="helix_voice_")
    os.close(handle)

    async def _run() -> None:
        await edge_tts.Communicate(text, voice, rate=rate).save(path)

    asyncio.run(_run())
    return path
