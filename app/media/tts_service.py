from __future__ import annotations

import os
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

# L-9: lazy-initialize — no client created at import time.
# The module can now be imported even when ELEVENLABS_API_KEY is missing
# (e.g. during testing or cold import in processes that won't call TTS).
_eleven: ElevenLabs | None = None


def _get_eleven() -> ElevenLabs:
    global _eleven
    if _eleven is None:
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set.")
        _eleven = ElevenLabs(api_key=api_key)
    return _eleven


def tts_to_mp3_bytes(text: str, voice_id: str) -> bytes:
    audio = _get_eleven().text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id="eleven_multilingual_v2",
        output_format="mp3_44100_192",
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
