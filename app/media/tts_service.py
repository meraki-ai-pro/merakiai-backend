from __future__ import annotations

import os
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

# L-9: lazy-initialize — no client created at import time.
# The module can now be imported even when ELEVENLABS_API_KEY is missing
# (e.g. during testing or cold import in processes that won't call TTS).
_eleven: ElevenLabs | None = None
_eleven_key: str | None = None  # the key the cached client was built with


def _get_eleven() -> ElevenLabs:
    # Rebuild the client if an admin rotated the key since it was created.
    global _eleven, _eleven_key
    from app.core.media_config import get_key
    api_key = get_key("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set.")
    if _eleven is None or api_key != _eleven_key:
        _eleven = ElevenLabs(api_key=api_key)
        _eleven_key = api_key
    return _eleven


# Low-latency model for the real-time avatar. eleven_turbo_v2_5 is ~5-10x
# faster than eleven_multilingual_v2 with very close quality — the right
# trade-off when the audio feeds a live D-ID stream. Overridable via env.
_TTS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5")
# 128 kbps is plenty for speech and uploads/generates a bit faster than 192.
_TTS_OUTPUT_FORMAT = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")


def tts_to_mp3_bytes(text: str, voice_id: str) -> bytes:
    audio = _get_eleven().text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id=_TTS_MODEL_ID,
        output_format=_TTS_OUTPUT_FORMAT,
        voice_settings=VoiceSettings(
            stability=0.60,
            similarity_boost=0.75,
            style=0.0,
            use_speaker_boost=True,
        ),
    )
    # SDK returns an iterator/stream-like; convert to bytes
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    return b"".join(audio)
