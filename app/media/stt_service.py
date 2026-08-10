# app/media/stt_service.py
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional
from openai import OpenAI

from fastapi import HTTPException

# L-9 style lazy init: build the client on first use and rebuild if an admin
# rotates the OpenAI key. Keeps the module importable without the key present.
_client: OpenAI | None = None
_client_key: str | None = None


def _get_client() -> OpenAI:
    global _client, _client_key
    from app.core.media_config import get_key
    api_key = get_key("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY")
    if _client is None or api_key != _client_key:
        _client = OpenAI(api_key=api_key)
        _client_key = api_key
    return _client

# Choose your model (Whisper via OpenAI hosted)
DEFAULT_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")


def _guess_suffix(filename: Optional[str]) -> str:
    """
    Preserve extension when possible because OpenAI uses it to infer format.
    """
    if filename:
        suf = Path(filename).suffix.lower()
        if suf in {".mp3", ".wav", ".m4a", ".webm", ".mp4", ".ogg"}:
            return suf
    return ".mp3"


def transcribe_audio(audio_bytes: bytes, filename: Optional[str] = None) -> str:
    """
    Windows-safe temp file handling:
    - write bytes to a temp path with delete=False
    - close it
    - reopen for OpenAI upload
    """
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload")

    suffix = _guess_suffix(filename)

    tmp_path = None
    try:
        # IMPORTANT: delete=False prevents Windows lock issues
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            tmp_path = tmp.name  # file is closed after 'with'

        with open(tmp_path, "rb") as f:
            # Option A: Transcriptions API
            # result = client.audio.transcriptions.create(
            #     model=DEFAULT_STT_MODEL,
            #     file=f
            # )

            # Option B: More general "transcribe"
            result = _get_client().audio.transcriptions.create(
                model=DEFAULT_STT_MODEL,
                file=f,
            )

        # New SDK returns object with .text (most commonly)
        text = getattr(result, "text", None)
        if not text:
            # fallback if result is dict-like
            text = result.get("text") if isinstance(result, dict) else None

        if not text:
            raise RuntimeError(f"Unexpected STT response: {result}")

        return text.strip()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"STT failed: {e}")

    finally:
        # cleanup temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass