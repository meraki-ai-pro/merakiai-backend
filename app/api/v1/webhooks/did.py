"""
D-ID webhook handler.

D-ID POSTs here when a clip finishes rendering (instead of the Celery worker polling).
The handler publishes the video URL to Redis → WebSocket manager → client.

Register in main.py:
    app.include_router(did_webhook_router)

Pass the webhook URL when creating a clip:
    webhook_url = f"{settings.PUBLIC_BASE_URL}/webhooks/did/{session_id}"
    clip_id = create_clip_async(presenter_id=..., audio_url=..., webhook_url=webhook_url)
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


@router.post("/did/{session_id}")
async def did_webhook(session_id: str, request: Request):
    """
    D-ID calls this endpoint when a clip is ready (or failed).

    Expected payload:
        { "status": "done", "result_url": "...", "subtitles_url": "..." }

    Security: D-ID sends the webhook_secret back as "Authorization: Bearer <secret>".
    We also accept it via ?secret=<value> query param as a fallback.
    """
    if _WEBHOOK_SECRET:
        # Check Authorization header (D-ID native approach)
        auth_header = request.headers.get("Authorization", "")
        expected = f"Bearer {_WEBHOOK_SECRET}"
        # Fallback: ?secret= query param (Tavus / generic approach)
        query_secret = request.query_params.get("secret", "")
        if auth_header != expected and query_secret != _WEBHOOK_SECRET:
            logger.warning("D-ID webhook rejected — invalid secret  session=%s", session_id)
            raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return {"received": True}

    status = (payload.get("status") or "").lower()
    logger.info("D-ID webhook  session=%s  status=%s", session_id, status)

    if status == "done":
        video_url = (
            payload.get("result_url")
            or payload.get("resultUrl")
            or payload.get("url")
        )
        subtitle_url = payload.get("subtitles_url", "")

        _publish(session_id, {
            "type": "video_ready",
            "response_format": "video",
            "video_url": video_url,
            "subtitle_url": subtitle_url,
            "source": "did",
        })

    elif status == "error":
        logger.warning("D-ID clip error  session=%s  payload=%s", session_id, payload)
        _publish(session_id, {
            "type": "video_error",
            "source": "did",
            "error": payload.get("error", {}).get("description", "D-ID generation failed"),
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
        logger.error("Failed to publish D-ID webhook result  session=%s  error=%s", session_id, exc)
