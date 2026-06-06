from __future__ import annotations

import asyncio
import os
import tempfile

# Free, natural, neural voices via edge-tts (Microsoft Edge "Read Aloud"). en-GB-RyanNeural
# is a British male voice that suits the J.A.R.V.I.S. tone.
DEFAULT_VOICE = "en-GB-RyanNeural"
DEFAULT_RATE = "+50%"  # ~1.5x speaking speed for the Xpert briefing


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
