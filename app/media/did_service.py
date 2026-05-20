# app/media/did_service.py
from __future__ import annotations

import os
import time
import requests
from typing import Any, Dict, Optional

DID_API_KEY = os.getenv("DID_API_KEY")
if not DID_API_KEY:
    raise RuntimeError("Missing DID_API_KEY")

# IMPORTANT: V3 Pro Avatars use /clips
DID_BASE_URL = "https://api.d-id.com"
CLIPS_URL = f"{DID_BASE_URL}/clips"

HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "authorization": f"Basic {DID_API_KEY}",
}

class DidError(RuntimeError):
    pass


def create_clip_from_audio(
    *,
    presenter_id: str,
    audio_url: str,
    title: Optional[str] = None,
    fluent: bool = True,
    stitch: bool = True,
    pad_audio: float = 0.5,
    result_format: str = "mp4",
    subtitle: bool = True,
    subtitles: bool = True,
    timeout_seconds: int = 360,      # increased default
    poll_interval: float = 2.5,
) -> Dict[str, Any]:
    """
    V3 Pro Avatars:
      POST  https://api.d-id.com/clips
      GET   https://api.d-id.com/clips/{id}
    """

    payload: Dict[str, Any] = {
        "presenter_id": presenter_id,
        "script": {
            "type": "audio",
            "audio_url": audio_url,
            "subtitles": subtitles,
        },
        "config": {
            "fluent": fluent,
            "stitch": stitch,
            "pad_audio": pad_audio,
            "result_format": result_format,
            "subtitle": subtitle
        },
    }
    if title:
        payload["title"] = title

    r = requests.post(CLIPS_URL, json=payload, headers=HEADERS, timeout=60)
    if r.status_code >= 400:
        raise DidError(f"D-ID API error [{r.status_code}]: {r.json() if _is_json(r) else r.text}\n"
                       f"Request payload: {payload}")

    data = r.json()
    clip_id = data.get("id")
    if not clip_id:
        # Some APIs return url; handle defensively
        clip_id = data.get("url") or data.get("clip_id")
    if not clip_id:
        raise DidError(f"D-ID create clip returned no id. Raw: {data}")

    # Poll until done
    result = wait_for_clip_result(
        clip_id=str(clip_id),
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )
    return result


def wait_for_clip_result(
    clip_id: str,
    timeout_seconds: int = 360,
    poll_interval: float = 2.5,
) -> Dict[str, Any]:
    """
    GET https://api.d-id.com/clips/{id}
    """
    status_url = f"{CLIPS_URL}/{clip_id}"
    deadline = time.time() + timeout_seconds

    last_payload: Optional[Dict[str, Any]] = None

    while time.time() < deadline:
        r = requests.get(status_url, headers=HEADERS, timeout=60)
        if r.status_code >= 400:
            raise DidError(f"D-ID status error [{r.status_code}]: {r.text}")

        data = r.json()
        last_payload = data

        status = (data.get("status") or "").lower().strip()
        if status == "done":
            # Different APIs name it differently; return whole payload
            return data

        if status == "error":
            raise DidError(f"D-ID clip failed: {data}")

        time.sleep(poll_interval)

    raise TimeoutError(f"Avatar generation timed out (clip_id={clip_id}). Last status: {last_payload}")


def create_clip_async(
    *,
    presenter_id: str,
    audio_url: str,
    webhook_url: str,
    webhook_secret: str | None = None,
    title: str | None = None,
    fluent: bool = True,
    stitch: bool = True,
    pad_audio: float = 0.5,
    result_format: str = "mp4",
    subtitles: bool = True,
) -> str:
    """
    Fire-and-forget variant: POST the clip to D-ID and return the clip_id immediately.
    D-ID will POST to webhook_url when processing is complete — no polling.

    If webhook_secret is provided, D-ID will include it as
    "Authorization: Bearer <secret>" in the webhook POST, allowing the handler
    to verify the request is genuinely from D-ID.

    Webhook payload on completion:
      { "status": "done", "result_url": "...", "subtitles_url": "..." }
    """
    payload: dict = {
        "presenter_id": presenter_id,
        "script": {
            "type": "audio",
            "audio_url": audio_url,
            "subtitles": subtitles,
        },
        "config": {
            "fluent": fluent,
            "stitch": stitch,
            "pad_audio": pad_audio,
            "result_format": result_format,
        },
        "webhook": webhook_url,
    }
    if webhook_secret:
        payload["webhook_secret"] = webhook_secret
    if title:
        payload["title"] = title

    r = requests.post(CLIPS_URL, json=payload, headers=HEADERS, timeout=60)
    if r.status_code >= 400:
        raise DidError(
            f"D-ID API error [{r.status_code}]: {r.json() if _is_json(r) else r.text}"
        )

    clip_id = r.json().get("id")
    if not clip_id:
        raise DidError(f"D-ID returned no clip id. Raw: {r.json()}")
    return clip_id


def _is_json(r: requests.Response) -> bool:
    ct = (r.headers.get("content-type") or "").lower()
    return "application/json" in ct