# app/media/video_service.py
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from fastapi import HTTPException
from app.db.supabase import get_supabase
from app.media.text_cleaner import clean_for_tts
from app.media.tts_service import tts_to_mp3_bytes
from app.media.storage_service import upload_audio_and_get_url
from app.media.did_agent_service import (
    DidAgentError,
    get_cached_stream,
    speak as did_agent_speak,
)
from app.media.did_service import create_clip_async, create_clip_from_audio, DidError
from app.media.tavus_service import create_tavus_video_async, create_tavus_video_from_audio, TavusError
from app.utils.srt_vtt import convert_srt_to_vtt

logger = logging.getLogger(__name__)

# Public base URL used to build webhook callback URLs.
# Set PUBLIC_BASE_URL in .env (e.g. https://api.yourdomain.com).
# If unset, falls back to polling mode for safety.
_PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# D-ID Agents streaming hard-rejects audio longer than 90s. This is a safety
# net on top of the concise video prompt: cap the spoken text well under that
# so speak() never fails with InvalidAudioFileDurationError. ~1500 chars is
# roughly 55-65s of TTS speech, leaving comfortable margin.
_MAX_SPEECH_CHARS = int(os.getenv("VIDEO_MAX_SPEECH_CHARS", "1500"))


def _clamp_for_speech(text: str) -> str:
    """Trim spoken text to stay under D-ID's 90s audio limit, on a clean boundary."""
    if len(text) <= _MAX_SPEECH_CHARS:
        return text
    head = text[:_MAX_SPEECH_CHARS]
    # Prefer to end on a sentence boundary within the budget.
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "), head.rfind("\n"))
    if cut < _MAX_SPEECH_CHARS * 0.5:
        cut = head.rfind(" ")  # no sentence break — fall back to a word boundary
    if cut <= 0:
        cut = _MAX_SPEECH_CHARS
    return text[: cut + 1].strip()


async def maybe_generate_video(
    *,
    text: str,
    response_format: str,
    user_id: str,
    session_id: str,
    mode: str,
):
    """
    Rules:
    - Only learn mode may return video (when user has prefers_video=True)
    - review and application ALWAYS return text
    - voice_id + presenter_id pulled from DB bundle (prevents gender mismatch)
    - D-ID is tried first; Tavus is the fallback if D-ID fails

    Optimizations applied:
      Opt 3 — Webhook-based dispatch: if PUBLIC_BASE_URL is configured, D-ID and Tavus
               are called in fire-and-forget mode. The worker finishes in ~5-10s instead
               of blocking for 20-60s. The webhook handler publishes the final video URL.
      Opt 4 — Parallel DB lookups: user profile + text cleaning run concurrently;
               bundle fetch + (future prep work) run concurrently.
    """

    mode = (mode or "").lower().strip()

    if mode != "learn":
        return {"response_format": "text", "video_url": None, "audio_url": None}

    if response_format != "video":
        return {"response_format": "text", "video_url": None, "audio_url": None}

    supabase = get_supabase()

    # ── Opt 4: fetch user profile and clean text concurrently ────────────────
    user_fut = asyncio.to_thread(
        lambda: supabase.table("users")
            .select("avatar_id,voice_id,avatar_gender,voice_gender")
            .eq("id", user_id)
            .execute()
    )
    clean_fut = asyncio.to_thread(clean_for_tts, text)

    u, clean = await asyncio.gather(user_fut, clean_fut)
    # ─────────────────────────────────────────────────────────────────────────

    if not u.data:
        raise HTTPException(status_code=404, detail="User profile not found")

    user_row = u.data[0]
    avatar_id = user_row.get("avatar_id")
    voice_id = user_row.get("voice_id")
    avatar_gender = user_row.get("avatar_gender")
    voice_gender = user_row.get("voice_gender")

    if not avatar_id or not voice_id:
        raise HTTPException(
            status_code=400,
            detail="User must select an avatar (with bundled voice) before using video responses.",
        )

    if not clean:
        return {"response_format": "text", "video_url": None, "audio_url": None}

    # Safety net: keep spoken audio under D-ID's 90s cap (the concise video
    # prompt already targets ~150 words; this guards against outliers).
    clean = _clamp_for_speech(clean)

    # ── Opt 4: fetch bundle concurrently while TTS is pending ────────────────
    # Bundle fetch can start immediately since we now have avatar_id.
    bundle_fut = asyncio.to_thread(
        lambda: supabase.table("avatar_voice_bundles")
            .select("did_presenter_id,is_active,avatar_gender,voice_gender,voice_id")
            .eq("avatar_id", avatar_id)
            .eq("is_active", True)
            .execute()
    )
    tts_fut = asyncio.to_thread(tts_to_mp3_bytes, clean, voice_id=voice_id)

    b, mp3 = await asyncio.gather(bundle_fut, tts_fut)
    # ─────────────────────────────────────────────────────────────────────────

    if not b.data:
        raise HTTPException(status_code=400, detail="Avatar bundle not configured or inactive")

    bundle = b.data[0]
    presenter_id = bundle["did_presenter_id"]

    if not presenter_id or not isinstance(presenter_id, str) or not presenter_id.strip():
        raise HTTPException(
            status_code=500,
            detail=f"Invalid presenter_id in database: {presenter_id!r}",
        )

    bundle_avatar_gender = bundle.get("avatar_gender")
    bundle_voice_gender = bundle.get("voice_gender")
    bundle_voice_id = bundle.get("voice_id")

    if bundle_voice_id and bundle_voice_id != voice_id:
        voice_id = bundle_voice_id

    if avatar_gender and bundle_avatar_gender and avatar_gender != bundle_avatar_gender:
        raise HTTPException(status_code=400, detail="Avatar gender mismatch vs bundle configuration")

    if voice_gender and bundle_voice_gender and voice_gender != bundle_voice_gender:
        raise HTTPException(status_code=400, detail="Voice gender mismatch vs bundle configuration")

    # Upload MP3 to Supabase Storage → public URL
    audio_url = await asyncio.to_thread(upload_audio_and_get_url, user_id, session_id, mp3)

    if not audio_url or not audio_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=500,
            detail=f"Invalid audio_url generated: {audio_url!r}",
        )

    # ── D-ID Agents real-time path (preferred when frontend has an active stream) ──
    stream_info = get_cached_stream(session_id)
    if stream_info:
        try:
            await asyncio.to_thread(
                did_agent_speak,
                stream_info["agent_id"],
                stream_info["stream_id"],
                audio_url=audio_url,
                did_session_id=stream_info["did_session_id"],
            )
            logger.info(
                "D-ID agent speak dispatched  session=%s  stream=%s",
                session_id, stream_info["stream_id"],
            )
            return {
                "response_format": "video",
                "audio_url": audio_url,
                "video_url": None,          # video delivered via live WebRTC stream
                "subtitle_url": "",
                "source": "did_agent",
                "streaming": True,
            }
        except DidAgentError as exc:
            logger.warning(
                "D-ID agent speak failed: %s — falling back to Tavus", exc
            )
    # ─────────────────────────────────────────────────────────────────────────

    use_webhooks = bool(_PUBLIC_BASE_URL)
    _webhook_secret = os.getenv("WEBHOOK_SECRET", "")

    # ── D-ID /clips async path (no WebRTC stream; generates a video file URL) ─
    try:
        if use_webhooks:
            if _webhook_secret:
                _did_token = hmac.new(
                    _webhook_secret.encode(), session_id.encode(), hashlib.sha256
                ).hexdigest()
                _webhook_url = f"{_PUBLIC_BASE_URL}/webhooks/did/{session_id}?token={_did_token}"
            else:
                _webhook_url = f"{_PUBLIC_BASE_URL}/webhooks/did/{session_id}"
            clip_id = await asyncio.to_thread(
                create_clip_async,
                presenter_id=presenter_id,
                audio_url=audio_url,
                webhook_url=_webhook_url,
                title=f"{mode} response",
            )
            logger.info("D-ID clip submitted (webhook)  session=%s  clip_id=%s", session_id, clip_id)
            return {
                "response_format": "video",
                "audio_url": audio_url,
                "video_url": None,
                "subtitle_url": "",
                "clip_id": clip_id,
                "pending": True,
                "source": "did_clips",
            }
        else:
            clip = await asyncio.to_thread(
                create_clip_from_audio,
                presenter_id=presenter_id,
                audio_url=audio_url,
                title=f"{mode} response",
                timeout_seconds=360,
            )
            video_url = (
                clip.get("result_url") or clip.get("resultUrl")
                or clip.get("url") or clip.get("result")
            )
            subtitle_url = clip.get("subtitles_url")
            vtt_data = convert_srt_to_vtt(subtitle_url) if subtitle_url else ""
            logger.info("D-ID clip ready (poll)  session=%s  url=%s", session_id, video_url)
            return {
                "response_format": "video",
                "audio_url": audio_url,
                "video_url": video_url,
                "subtitle_url": vtt_data,
                "source": "did_clips",
            }
    except DidError as clips_err:
        logger.warning("D-ID clips failed: %s — falling back to Tavus", clips_err)

    # ── Tavus fallback (ultimate fallback) ───────────────────────────────────
    logger.info("Falling back to Tavus  session=%s  webhooks=%s", session_id, use_webhooks)
    env_var_name = f"TAVUS_{avatar_id.upper()}_REPLICA_ID"
    replica_id = os.getenv(env_var_name) or os.getenv("TAVUS_DEFAULT_REPLICA_ID", "")

    try:
        if use_webhooks:
            if _webhook_secret:
                _token = hmac.new(
                    _webhook_secret.encode(), session_id.encode(), hashlib.sha256
                ).hexdigest()
                callback_url = f"{_PUBLIC_BASE_URL}/webhooks/tavus/{session_id}?token={_token}"
            else:
                callback_url = f"{_PUBLIC_BASE_URL}/webhooks/tavus/{session_id}"
            video_id = await asyncio.to_thread(
                create_tavus_video_async,
                replica_id=replica_id,
                audio_url=audio_url,
                callback_url=callback_url,
                video_name=f"{mode} response",
            )
            logger.info("Tavus video submitted (webhook)  session=%s  video_id=%s",
                        session_id, video_id)
            return {
                "response_format": "video",
                "audio_url": audio_url,
                "video_url": None,
                "subtitle_url": "",
                "video_id": video_id,
                "pending": True,
                "source": "tavus",
            }
        else:
            tavus_video = await asyncio.to_thread(
                create_tavus_video_from_audio,
                replica_id=replica_id,
                audio_url=audio_url,
                video_name=f"{mode} response",
            )
            video_url = tavus_video.get("download_url") or tavus_video.get("hosted_url")
            logger.info("Tavus video ready (poll). URL: %s", video_url)
            return {
                "response_format": "video",
                "audio_url": audio_url,
                "video_url": video_url,
                "subtitle_url": "",
                "source": "tavus",
            }
    except Exception as tavus_err:
        logger.error("Tavus video generation failed: %s", tavus_err)
        return {"response_format": "text", "video_url": None, "audio_url": audio_url}
