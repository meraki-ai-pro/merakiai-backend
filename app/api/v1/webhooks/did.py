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

import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets

import redis as redis_sync
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def _get_webhook_secret() -> str:
    """Return the webhook secret, raising 500 at request time if unset.

    Reading from os.environ at request time (rather than module level) ensures
    the value is always picked up after load_env() has run at startup.
    """
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        logger.error("WEBHOOK_SECRET is not configured — rejecting webhook call")
        raise HTTPException(
            status_code=500,
            detail="Webhook endpoint is not properly configured on the server.",
        )
    return secret


@router.post("/did/{session_id}")
async def did_webhook(session_id: str, request: Request):
    """
    D-ID calls this endpoint when a clip is ready (or failed).

    Expected payload:
        { "status": "done", "result_url": "...", "subtitles_url": "..." }

    Security: we embed an HMAC-SHA256(WEBHOOK_SECRET, session_id) token in the
    webhook URL query string when registering with D-ID, then verify it here.
    Constant-time comparison prevents timing side-channel attacks.
    """
    # C-3: auth is always enforced — 500 if WEBHOOK_SECRET is not set.
    webhook_secret = _get_webhook_secret()

    # Primary: HMAC token in ?token= query param (set when registering the webhook URL).
    token = request.query_params.get("token", "")
    if token:
        expected_token = hmac.new(
            webhook_secret.encode(), session_id.encode(), hashlib.sha256
        ).hexdigest()
        if not _secrets.compare_digest(token, expected_token):
            logger.warning("D-ID webhook rejected — invalid HMAC token  session=%s", session_id)
            raise HTTPException(status_code=401, detail="Unauthorized")
    else:
        # Fallback: legacy Bearer header (kept for backward compatibility).
        auth_header = request.headers.get("Authorization", "")
        if not _secrets.compare_digest(auth_header, f"Bearer {webhook_secret}"):
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
