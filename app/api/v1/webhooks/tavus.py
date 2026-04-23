"""
Tavus webhook handler.

Tavus POSTs here when a video finishes rendering (instead of the Celery worker polling).
The handler publishes the video URL to Redis → WebSocket manager → client.

Register in main.py:
    app.include_router(tavus_webhook_router)

Pass the webhook URL when creating a video:
    callback_url = f"{settings.PUBLIC_BASE_URL}/webhooks/tavus/{session_id}"
    video_id = create_tavus_video_async(
        replica_id=..., audio_url=..., callback_url=callback_url
    )

Tavus webhook payload on completion (status "ready"):
    {
      "video_id": "...",
      "status": "ready",
      "download_url": "https://stream.mux.com/.../high.mp4?download=...",
      "hosted_url": "https://videos.tavus.io/video/...",
      "stream_url": "https://stream.mux.com/....m3u8",
      "status_details": "Your request has processed successfully!"
    }
"""
from __future__ import annotations

import json
import logging
import os

import redis as redis_sync
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

_WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


@router.post("/tavus/{session_id}")
async def tavus_webhook(session_id: str, request: Request):
    """
    Tavus calls this endpoint when a video is ready (or failed).

    Key payload fields:
        status        — "ready" | "error" | "queued" | "generating"
        download_url  — direct MP4 download link
        hosted_url    — hosted player page URL
        stream_url    — HLS stream (.m3u8)
        status_details — human-readable message
    """
    if _WEBHOOK_SECRET:
        # Tavus passes the secret via ?secret= query param in the callback URL
        query_secret = request.query_params.get("secret", "")
        if query_secret != _WEBHOOK_SECRET:
            logger.warning("Tavus webhook rejected — invalid secret  session=%s", session_id)
            raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return {"received": True}

    status = (payload.get("status") or "").lower()
    logger.info("Tavus webhook  session=%s  status=%s", session_id, status)

    if status == "ready":
        # Prefer download_url for direct playback; fall back to hosted_url
        video_url = (
            payload.get("download_url")
            or payload.get("hosted_url")
            or payload.get("stream_url")
        )
        _publish(session_id, {
            "type": "video_ready",
            "response_format": "video",
            "video_url": video_url,
            "subtitle_url": "",   # Tavus BYOA does not generate VTT subtitles
            "source": "tavus",
        })

    elif status == "error":
        logger.warning("Tavus video error  session=%s  details=%s",
                       session_id, payload.get("status_details"))
        _publish(session_id, {
            "type": "video_error",
            "source": "tavus",
            "error": payload.get("status_details", "Tavus generation failed"),
        })

    return {"received": True}


def _publish(session_id: str, message: dict) -> None:
    try:
        r = redis_sync.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        r.publish(f"ws:{session_id}", json.dumps(message))
        r.close()
    except Exception as exc:
        logger.error("Failed to publish Tavus webhook result  session=%s  error=%s",
                     session_id, exc)
