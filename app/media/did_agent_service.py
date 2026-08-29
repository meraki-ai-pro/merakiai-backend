"""
D-ID Agents API service — replaces the legacy /clips endpoint.

Architecture:
  1. One D-ID agent is created per presenter_id (cached in Redis for 7 days).
     Agent creation is a one-time operation. If the agent is deleted in D-ID
     Studio the cache key must also be cleared.

  2. Per session: create a WebRTC stream for the agent. The frontend completes
     the SDP/ICE handshake via our /api/v1/video endpoints. Stream info is
     stored in Redis under  did_stream:{session_id}  with a 1-hour TTL.

  3. Per message: call speak() with the pre-generated ElevenLabs audio URL.
     The avatar renders the audio in real-time via the live WebRTC connection.

  4. On session end (or WebSocket disconnect): close the stream.

D-ID Agents API base URL: https://api.d-id.com/agents
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

import redis as redis_sync
import requests

logger = logging.getLogger(__name__)

DID_BASE_URL = os.getenv("DID_BASE_URL", "https://api.d-id.com")
DID_AGENT_LLM_MODEL = os.getenv("DID_AGENT_LLM_MODEL", "gpt-4.1-mini")


def _headers() -> dict:
    """D-ID auth headers built at call time so a rotated key applies live."""
    from app.core.media_config import get_key
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Basic {get_key('DID_API_KEY') or ''}",
    }

_AGENT_CACHE_TTL = 7 * 24 * 3600   # 7 days — agents persist in D-ID account
_STREAM_CACHE_TTL = 3600            # 1 hour — stream session lifetime


class DidAgentError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Redis helper (re-uses same pattern as tasks.py)
# ---------------------------------------------------------------------------

_redis_client: redis_sync.Redis | None = None


def _get_redis() -> redis_sync.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_sync.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
    return _redis_client


# ---------------------------------------------------------------------------
# Agent management (one agent per presenter_id)
# ---------------------------------------------------------------------------

def get_or_create_agent(presenter_id: str) -> str:
    """
    Return the D-ID agent_id for the given presenter, creating one if needed.
    Cached in Redis for 7 days — agents are persistent in D-ID accounts.
    """
    cache_key = f"did_agent:{presenter_id}"
    try:
        cached = _get_redis().get(cache_key)
        if cached:
            return cached
    except Exception:
        pass

    agent_id = _create_agent(presenter_id)

    try:
        _get_redis().setex(cache_key, _AGENT_CACHE_TTL, agent_id)
    except Exception:
        pass

    return agent_id


def _create_agent(presenter_id: str) -> str:
    """
    POST /agents

    Creates a D-ID agent with a V3 Pro Avatar presenter.
    The LLM is required by D-ID but is bypassed when using speak() with
    pre-generated ElevenLabs audio — only the avatar renderer is used.
    """
    from app.core.media_config import get_key
    openai_key = get_key("OPENAI_API_KEY") or ""

    payload: Dict[str, Any] = {
        "presenter": {
            "type": "clip",           # V3 Pro Avatar (same product as /clips)
            "presenter_id": presenter_id,
        },
        "llm": {
            "type": "openai",
            "provider": "openai",
            "model": DID_AGENT_LLM_MODEL,
            "api_key": openai_key,
            "instructions": "You are a helpful AI tutor. Answer concisely.",
        },
        "preview_name": f"Meraki Tutor ({presenter_id[:8]})",
    }

    r = requests.post(
        f"{DID_BASE_URL}/agents",
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    if r.status_code >= 400:
        raise DidAgentError(
            f"D-ID create agent [{r.status_code}]: "
            f"{_safe_response(r)}"
        )

    data = r.json()
    agent_id = data.get("id") or data.get("agent_id")
    if not agent_id:
        raise DidAgentError(f"D-ID create agent returned no id. Raw: {data}")

    logger.info("Created D-ID agent  id=%s  presenter=%s", agent_id, presenter_id)
    return str(agent_id)


# ---------------------------------------------------------------------------
# Stream lifecycle
# ---------------------------------------------------------------------------

def create_stream(
    agent_id: str,
    *,
    compatibility_mode: str = "auto",
    stream_warmup: bool = True,
) -> Dict[str, Any]:
    """
    POST /agents/{agentId}/streams

    Initiates a WebRTC connection.  Response shape:
      {
        "id": "<stream_id>",
        "session_id": "<d-id-session-id>",
        "offer": {"type": "offer", "sdp": "<sdp>"},
        "ice_servers": [{"urls": [...], "username": "...", "credential": "..."}]
      }

    Note: session_timeout is a plan-gated field — omit it to avoid 403.
    """
    payload: Dict[str, Any] = {
        "compatibility_mode": compatibility_mode,
        "stream_warmup": stream_warmup,
    }

    r = requests.post(
        f"{DID_BASE_URL}/agents/{agent_id}/streams",
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    if r.status_code >= 400:
        raise DidAgentError(
            f"D-ID create stream [{r.status_code}]: "
            f"{_safe_response(r)}"
        )

    data = r.json()
    stream_id = data.get("id") or data.get("stream_id")
    if not stream_id:
        raise DidAgentError(f"D-ID create stream returned no id. Raw: {data}")

    logger.info("Created D-ID stream  agent=%s  stream=%s", agent_id, stream_id)
    return data


def cache_stream(app_session_id: str, agent_id: str, stream_data: Dict[str, Any]) -> None:
    """Store stream connection info in Redis for later speak() calls."""
    payload = {
        "agent_id": agent_id,
        "stream_id": stream_data.get("id") or stream_data.get("stream_id"),
        "did_session_id": stream_data.get("session_id", ""),
    }
    try:
        _get_redis().setex(
            f"did_stream:{app_session_id}",
            _STREAM_CACHE_TTL,
            json.dumps(payload),
        )
    except Exception as exc:
        logger.warning("Failed to cache D-ID stream info: %s", exc)


def get_cached_stream(app_session_id: str) -> Optional[Dict[str, str]]:
    """Return cached stream info or None if no active stream."""
    try:
        raw = _get_redis().get(f"did_stream:{app_session_id}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def submit_sdp_answer(
    agent_id: str,
    stream_id: str,
    *,
    answer_sdp: str,
    sdp_type: str = "answer",
    did_session_id: str,
) -> None:
    """
    POST /agents/{agentId}/streams/{streamId}/sdp

    Forwards the WebRTC SDP answer from the frontend to D-ID to complete the
    peer connection handshake.
    """
    payload = {
        "answer": {"type": sdp_type, "sdp": answer_sdp},
        "session_id": did_session_id,
    }
    r = requests.post(
        f"{DID_BASE_URL}/agents/{agent_id}/streams/{stream_id}/sdp",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise DidAgentError(
            f"D-ID SDP answer [{r.status_code}]: "
            f"{_safe_response(r)}"
        )
    logger.debug("SDP answer submitted  agent=%s  stream=%s", agent_id, stream_id)


def submit_ice_candidate(
    agent_id: str,
    stream_id: str,
    *,
    candidate: str | None,
    sdp_mid: str | None,
    sdp_mline_index: int | None,
    did_session_id: str,
) -> None:
    """
    POST /agents/{agentId}/streams/{streamId}/ice

    Forwards a WebRTC ICE candidate from the frontend to D-ID.

    The body is FLAT — candidate, sdpMid and sdpMLineIndex are siblings of
    session_id, exactly as in D-ID's own reference client
    (de-id/live-streaming-demo, streaming-client-api.js::onIceCandidate). This
    was previously sent nested under a "candidate" object; D-ID answers 200
    either way, so nothing failed loudly — it simply never learned our
    candidates, no pair was ever formed, and ICE stalled at 'checking' before
    dropping to 'disconnected'.

    A null candidate is the end-of-candidates signal and is sent as session_id
    alone, again per the reference. Without it D-ID keeps waiting for more
    candidates instead of proceeding with the ones it has.
    """
    if candidate is None:
        payload: dict = {"session_id": did_session_id}
    else:
        payload = {
            "candidate": candidate,
            "sdpMid": sdp_mid or "",
            "sdpMLineIndex": sdp_mline_index or 0,
            "session_id": did_session_id,
        }
    r = requests.post(
        f"{DID_BASE_URL}/agents/{agent_id}/streams/{stream_id}/ice",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise DidAgentError(
            f"D-ID ICE candidate [{r.status_code}]: "
            f"{_safe_response(r)}"
        )
    logger.debug("ICE candidate submitted  agent=%s  stream=%s", agent_id, stream_id)


def speak(
    agent_id: str,
    stream_id: str,
    *,
    audio_url: str,
    did_session_id: str,
) -> None:
    """
    POST /agents/{agentId}/streams/{streamId}

    Sends pre-generated ElevenLabs audio to the avatar.
    The avatar renders the audio in real-time via the WebRTC connection — no
    video file is generated and no polling is required.
    """
    payload: Dict[str, Any] = {
        "script": {
            "type": "audio",
            "audio_url": audio_url,
        },
        "session_id": did_session_id,
    }
    r = requests.post(
        f"{DID_BASE_URL}/agents/{agent_id}/streams/{stream_id}",
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    if r.status_code >= 400:
        raise DidAgentError(
            f"D-ID speak [{r.status_code}]: "
            f"{_safe_response(r)}"
        )
    logger.info(
        "D-ID speak dispatched  agent=%s  stream=%s  audio_url_set=%s",
        agent_id, stream_id, bool(audio_url),
    )


def close_stream(agent_id: str, stream_id: str, did_session_id: str) -> None:
    """
    DELETE /agents/{agentId}/streams/{streamId}

    Releases D-ID resources.  Errors are logged but not re-raised so that
    session teardown is never blocked by a failing cleanup call.
    """
    try:
        r = requests.delete(
            f"{DID_BASE_URL}/agents/{agent_id}/streams/{stream_id}",
            json={"session_id": did_session_id},
            headers=_headers(),
            timeout=10,
        )
        if r.status_code >= 400:
            logger.warning(
                "D-ID close stream %d  agent=%s  stream=%s",
                r.status_code, agent_id, stream_id,
            )
        else:
            logger.info("Closed D-ID stream  agent=%s  stream=%s", agent_id, stream_id)
    except Exception as exc:
        logger.warning("Failed to close D-ID stream: %s", exc)


def evict_stream_cache(app_session_id: str) -> None:
    """Remove stream cache entry — call on session end."""
    try:
        _get_redis().delete(f"did_stream:{app_session_id}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_json(r: requests.Response) -> bool:
    return "application/json" in (r.headers.get("content-type") or "").lower()


_SECRET_KEY_PARTS = ("api_key", "authorization", "access_token", "secret", "token")
_OPENAI_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{20,}")


def _redact_secrets(value: Any) -> Any:
    """Recursively remove credentials third-party validation errors echo."""
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
                else _redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _OPENAI_KEY_PATTERN.sub("[REDACTED]", value)
    return value


def _safe_response(r: requests.Response) -> Any:
    try:
        body: Any = r.json() if _is_json(r) else r.text
    except Exception:
        body = r.text
    return _redact_secrets(body)
