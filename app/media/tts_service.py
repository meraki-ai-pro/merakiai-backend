import os
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs
from app.config import load_env

load_env()

eleven = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

def tts_to_mp3_bytes(text: str, voice_id: str) -> bytes:
    audio = eleven.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id="eleven_multilingual_v2",
        output_format="mp3_44100_192",
        voice_settings=VoiceSettings(
            stability=0.60,
            similarity_boost=0.75,
            style=0.0,
            use_speaker_boost=True
        )
    )
    # SDK returns an iterator/stream-like; convert to bytes
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    return b"".join(audio)
