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
from app.media.did_service import create_clip_async, create_clip_from_audio, DidError
from app.media.tavus_service import create_tavus_video_async, create_tavus_video_from_audio, TavusError
from app.utils.srt_vtt import convert_srt_to_vtt

logger = logging.getLogger(__name__)

# Public base URL used to build webhook callback URLs.
# Set PUBLIC_BASE_URL in .env (e.g. https://api.yourdomain.com).
# If unset, falls back to polling mode for safety.
_PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


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

    use_webhooks = bool(_PUBLIC_BASE_URL)
    _webhook_secret = os.getenv("WEBHOOK_SECRET", "")

    # ── Try D-ID (primary) ───────────────────────────────────────────────────
    logger.info("Creating D-ID clip  session=%s  presenter=%s  webhooks=%s",
                session_id, presenter_id, use_webhooks)

    clip = None
    use_tavus = False

    try:
        if use_webhooks:
            # Opt 3: fire-and-forget — worker task ends here, webhook delivers the video
            webhook_url = f"{_PUBLIC_BASE_URL}/webhooks/did/{session_id}"
            clip_id = await asyncio.to_thread(
                create_clip_async,
                presenter_id=presenter_id,
                audio_url=audio_url,
                webhook_url=webhook_url,
                webhook_secret=_webhook_secret or None,
                title=f"{mode} response",
            )
            logger.info("D-ID clip submitted (webhook)  session=%s  clip_id=%s", session_id, clip_id)
            return {
                "response_format": "video",
                "audio_url": audio_url,
                "video_url": None,        # delivered via webhook when ready
                "subtitle_url": "",
                "clip_id": clip_id,
                "pending": True,
            }
        else:
            # Fallback: blocking poll (used when PUBLIC_BASE_URL is not set)
            clip = await asyncio.to_thread(
                create_clip_from_audio,
                presenter_id=presenter_id,
                audio_url=audio_url,
                title=f"{mode} response",
                timeout_seconds=360,
            )
    except Exception as e:
        logger.warning("D-ID generation failed: %s. Falling back to Tavus...", e)
        use_tavus = True

    # ── Tavus fallback ───────────────────────────────────────────────────────
    if use_tavus:
        env_var_name = f"TAVUS_{avatar_id.upper()}_REPLICA_ID"
        replica_id = os.getenv(env_var_name) or os.getenv("TAVUS_DEFAULT_REPLICA_ID", "")

        try:
            if use_webhooks:
                # Opt 3: fire-and-forget
                # M-7: pass an HMAC-SHA256 token instead of the raw secret so the
                # secret is never visible in URL logs or server-side access logs.
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
                    video_name=f"{mode} fallback response",
                )
                logger.info("Tavus video submitted (webhook)  session=%s  video_id=%s",
                            session_id, video_id)
                return {
                    "response_format": "video",
                    "audio_url": audio_url,
                    "video_url": None,    # delivered via webhook when ready
                    "subtitle_url": "",
                    "video_id": video_id,
                    "pending": True,
                    "source": "tavus",
                }
            else:
                # Fallback: blocking poll
                tavus_video = await asyncio.to_thread(
                    create_tavus_video_from_audio,
                    replica_id=replica_id,
                    audio_url=audio_url,
                    video_name=f"{mode} fallback response",
                )
                video_url = tavus_video.get("download_url") or tavus_video.get("hosted_url")
                logger.info("Tavus fallback video ready (poll). URL: %s", video_url)
                return {
                    "response_format": "video",
                    "audio_url": audio_url,
                    "video_url": video_url,
                    "subtitle_url": "",
                    "did_payload": tavus_video,
                }
        except Exception as tavus_err:
            logger.error("Tavus fallback also failed: %s", tavus_err)
            # Both providers failed — return text gracefully
            return {"response_format": "text", "video_url": None, "audio_url": audio_url}

    # ── D-ID polling success path ────────────────────────────────────────────
    video_url = (
        clip.get("result_url")
        or clip.get("resultUrl")
        or clip.get("url")
        or clip.get("result")
    )
    subtitle_url = clip.get("subtitles_url")
    vtt_data = convert_srt_to_vtt(subtitle_url) if subtitle_url else ""
    logger.info("D-ID video ready (poll). Subtitles: %s", bool(subtitle_url))

    return {
        "response_format": "video",
        "audio_url": audio_url,
        "video_url": video_url,
        "subtitle_url": vtt_data,
        "did_payload": clip,
    }
