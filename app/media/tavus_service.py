import os
import time
import requests
from typing import Any, Dict, Optional

from app.config import load_env

load_env()

TAVUS_API_KEY = os.getenv("TAVUS_API_KEY")
TAVUS_DEFAULT_REPLICA_ID = os.getenv("TAVUS_DEFAULT_REPLICA_ID")

TAVUS_BASE_URL = "https://tavusapi.com/v2"


class TavusError(RuntimeError):
    pass


def get_tavus_headers() -> dict:
    if not TAVUS_API_KEY:
        raise TavusError("Missing TAVUS_API_KEY in environment variables.")
    return {
        "x-api-key": TAVUS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_tavus_video_from_audio(
    *,
    replica_id: str,
    audio_url: str,
    video_name: Optional[str] = None,
    timeout_seconds: int = 600,
    poll_interval: float = 5.0,
) -> Dict[str, Any]:
    """
    Generate a video using Tavus V2 API.
    POST https://tavusapi.com/v2/videos
    """
    if not replica_id and TAVUS_DEFAULT_REPLICA_ID:
        replica_id = TAVUS_DEFAULT_REPLICA_ID

    if not replica_id:
        raise TavusError("Missing replica_id and TAVUS_DEFAULT_REPLICA_ID is not set.")

    payload: Dict[str, Any] = {
        "replica_id": replica_id,
        "audio_url": audio_url,   # BYOA: bring-your-own-audio (ElevenLabs TTS)
        "fast": True,             # faster rendering pipeline
    }
    if video_name:
        payload["video_name"] = video_name

    headers = get_tavus_headers()

    url = f"{TAVUS_BASE_URL}/videos"
    r = requests.post(url, json=payload, headers=headers, timeout=60)

    if r.status_code >= 400:
        error_msg = r.text
        try:
            error_msg = r.json()
        except Exception:
            pass
        raise TavusError(f"Tavus API error [{r.status_code}]: {error_msg}")

    data = r.json()
    video_id = data.get("video_id")
    if not video_id:
        raise TavusError(f"Tavus create video returned no video_id. Raw: {data}")

    return wait_for_tavus_video_result(
        video_id=str(video_id),
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )


def create_tavus_video_async(
    *,
    replica_id: str,
    audio_url: str,
    callback_url: str,
    video_name: str | None = None,
) -> str:
    """
    Fire-and-forget variant: POST the video request to Tavus and return the video_id immediately.
    Tavus will POST to callback_url when rendering is complete — no polling.

    Webhook payload on completion (status "ready"):
      {
        "video_id": "...",
        "status": "ready",
        "download_url": "https://stream.mux.com/.../high.mp4?download=...",
        "hosted_url": "https://videos.tavus.io/video/...",
        "stream_url": "https://stream.mux.com/....m3u8",
        "status_details": "Your request has processed successfully!"
      }

    On error the payload will have status != "ready" and status_details with the error message.
    """
    if not replica_id and TAVUS_DEFAULT_REPLICA_ID:
        replica_id = TAVUS_DEFAULT_REPLICA_ID
    if not replica_id:
        raise TavusError("Missing replica_id and TAVUS_DEFAULT_REPLICA_ID is not set.")

    payload: dict = {
        "replica_id": replica_id,
        "audio_url": audio_url,   # BYOA: bring-your-own-audio (ElevenLabs TTS)
        "callback_url": callback_url,
        "fast": True,             # faster rendering pipeline (~30-60s vs 3-10min)
    }
    if video_name:
        payload["video_name"] = video_name

    headers = get_tavus_headers()
    r = requests.post(f"{TAVUS_BASE_URL}/videos", json=payload, headers=headers, timeout=60)
    if r.status_code >= 400:
        try:
            error_msg = r.json()
        except Exception:
            error_msg = r.text
        raise TavusError(f"Tavus API error [{r.status_code}]: {error_msg}")

    video_id = r.json().get("video_id")
    if not video_id:
        raise TavusError(f"Tavus create video returned no video_id. Raw: {r.json()}")
    return video_id


def wait_for_tavus_video_result(
    video_id: str,
    timeout_seconds: int = 600,
    poll_interval: float = 5.0,
) -> Dict[str, Any]:
    """
    Poll the Tavus API until the video is ready.
    GET https://tavusapi.com/v2/videos/{video_id}
    """
    status_url = f"{TAVUS_BASE_URL}/videos/{video_id}"
    headers = get_tavus_headers()
    deadline = time.time() + timeout_seconds
    last_payload = None

    while time.time() < deadline:
        r = requests.get(status_url, headers=headers, timeout=60)
        if r.status_code >= 400:
            raise TavusError(f"Tavus status error [{r.status_code}]: {r.text}")

        data = r.json()
        last_payload = data

        status = (data.get("status") or "").lower().strip()

        # Tavus statuses: 'queued', 'generating', 'ready', 'error'
        if status == "ready":
            return data

        if status in ("error", "failed"):
            raise TavusError(f"Tavus video generation failed: {data}")

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Tavus video generation timed out (video_id={video_id}). Last status: {last_payload}"
    )
