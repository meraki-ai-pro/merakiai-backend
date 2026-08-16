"""
Video streaming endpoints — WebRTC signaling proxy for D-ID Agents.

The frontend calls these endpoints to establish and manage a WebRTC connection
with D-ID.  Once the connection is live, the backend calls
did_agent_service.speak() from within Celery tasks whenever audio is ready.

Endpoint flow:
  POST  /api/v1/video/stream            → create stream, receive SDP offer
  POST  /api/v1/video/stream/{id}/sdp   → submit SDP answer
  POST  /api/v1/video/stream/{id}/ice   → submit ICE candidates
  DELETE /api/v1/video/stream/{id}      → close stream
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import auth_guard
from app.db.supabase import get_user_client
from app.media.did_agent_service import (
    DidAgentError,
    cache_stream,
    close_stream,
    create_stream,
    evict_stream_cache,
    get_cached_stream,
    get_or_create_agent,
    submit_ice_candidate,
    submit_sdp_answer,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/video", tags=["Video"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class StreamCreateRequest(BaseModel):
    session_id: str = Field(..., description="App session UUID")


class StreamCreateResponse(BaseModel):
    stream_id: str
    did_session_id: str
    offer: dict                     # {type: "offer", sdp: "..."}
    ice_servers: list[dict]         # [{urls, username, credential}]


class SdpAnswerRequest(BaseModel):
    session_id: str
    answer_sdp: str = Field(..., description="WebRTC SDP answer string from RTCPeerConnection")
    sdp_type: str = Field("answer", description="SDP type, always 'answer'")


class IceCandidateRequest(BaseModel):
    session_id: str
    # All three are optional so the client can post the end-of-candidates
    # signal (a null candidate), which D-ID needs in order to stop waiting for
    # more and finish negotiating with what it already has.
    candidate: str | None = None
    sdp_mid: str | None = None
    sdp_mline_index: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_presenter_id(user: dict) -> str:
    """Resolve avatar_id → did_presenter_id for the authenticated user."""
    supabase = get_user_client(user["token"])

    user_res = await asyncio.to_thread(
        lambda: supabase.table("users")
            .select("avatar_id")
            .eq("id", user["id"])
            .execute()
    )
    if not user_res.data or not user_res.data[0].get("avatar_id"):
        raise HTTPException(status_code=400, detail="User has no avatar configured")

    avatar_id = user_res.data[0]["avatar_id"]

    bundle_res = await asyncio.to_thread(
        lambda: supabase.table("avatar_voice_bundles")
            .select("did_presenter_id")
            .eq("avatar_id", avatar_id)
            .eq("is_active", True)
            .execute()
    )
    if not bundle_res.data or not bundle_res.data[0].get("did_presenter_id"):
        raise HTTPException(status_code=400, detail="No active avatar bundle found")

    return bundle_res.data[0]["did_presenter_id"]


def _require_stream(session_id: str) -> dict:
    stream_info = get_cached_stream(session_id)
    if not stream_info:
        raise HTTPException(
            status_code=404,
            detail="No active video stream for this session. Call POST /stream first.",
        )
    return stream_info


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/stream", response_model=StreamCreateResponse)
async def create_video_stream(
    payload: StreamCreateRequest,
    user=Depends(auth_guard),
):
    """
    Create a D-ID WebRTC stream for the session.

    Returns SDP offer + ICE servers the frontend needs to establish a peer
    connection with D-ID.  The stream info is cached server-side so that
    later speak() calls from Celery workers can find it by session_id.
    """
    presenter_id = await _get_presenter_id(user)

    try:
        agent_id = await asyncio.to_thread(get_or_create_agent, presenter_id)
        stream_data = await asyncio.to_thread(create_stream, agent_id)
    except DidAgentError as exc:
        logger.error("D-ID stream creation failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"D-ID error: {exc}")

    cache_stream(payload.session_id, agent_id, stream_data)

    return StreamCreateResponse(
        stream_id=stream_data.get("id") or stream_data.get("stream_id"),
        did_session_id=stream_data.get("session_id", ""),
        offer=stream_data.get("offer", {}),
        ice_servers=stream_data.get("ice_servers", []),
    )


@router.post("/stream/{stream_id}/sdp")
async def submit_stream_sdp(
    stream_id: str,
    payload: SdpAnswerRequest,
    user=Depends(auth_guard),
):
    """
    Submit the WebRTC SDP answer from the frontend to D-ID.

    Call this after RTCPeerConnection.createAnswer() to complete the handshake.
    """
    stream_info = _require_stream(payload.session_id)

    try:
        await asyncio.to_thread(
            submit_sdp_answer,
            stream_info["agent_id"],
            stream_id,
            answer_sdp=payload.answer_sdp,
            sdp_type=payload.sdp_type,
            did_session_id=stream_info["did_session_id"],
        )
    except DidAgentError as exc:
        raise HTTPException(status_code=502, detail=f"D-ID SDP error: {exc}")

    return {"status": "ok"}


@router.post("/stream/{stream_id}/ice")
async def submit_stream_ice(
    stream_id: str,
    payload: IceCandidateRequest,
    user=Depends(auth_guard),
):
    """
    Submit a WebRTC ICE candidate from the frontend to D-ID.

    Collect candidates from RTCPeerConnection.onicecandidate and POST each one.
    """
    stream_info = _require_stream(payload.session_id)

    try:
        await asyncio.to_thread(
            submit_ice_candidate,
            stream_info["agent_id"],
            stream_id,
            candidate=payload.candidate,
            sdp_mid=payload.sdp_mid,
            sdp_mline_index=payload.sdp_mline_index,
            did_session_id=stream_info["did_session_id"],
        )
    except DidAgentError as exc:
        raise HTTPException(status_code=502, detail=f"D-ID ICE error: {exc}")

    return {"status": "ok"}


@router.delete("/stream/{stream_id}")
async def close_video_stream(
    stream_id: str,
    session_id: str = Query(..., description="App session UUID"),
    user=Depends(auth_guard),
):
    """
    Close the D-ID WebRTC stream and release resources.

    Call on session end or when the user navigates away.
    """
    stream_info = get_cached_stream(session_id)
    if stream_info:
        await asyncio.to_thread(
            close_stream,
            stream_info["agent_id"],
            stream_id,
            stream_info["did_session_id"],
        )
        evict_stream_cache(session_id)

    return {"status": "closed"}
