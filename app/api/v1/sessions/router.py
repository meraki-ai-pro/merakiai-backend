from __future__ import annotations

import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import auth_guard
from app.db.supabase import get_user_client
from app.models.models import VideoToggle, SessionModeUpdate, SessionCreateRequest, SessionTitleUpdate

router = APIRouter(prefix="/sessions", tags=["Sessions"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid session ID format")


@router.get("/")
def list_sessions(
    user=Depends(auth_guard),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return all sessions for the authenticated user, newest first."""
    supabase = get_user_client(user["token"])
    rows = (
        supabase.table("sessions")
        .select("id, current_mode, prefers_video, started_at, ended_at, course_id")
        .eq("user_id", user["id"])
        .order("started_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    sessions = []
    for row in (rows.data or []):
        sid = row["id"]
        first_msg = (
            supabase.table("conversations")
            .select("user_input")
            .eq("session_id", sid)
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        title = None
        if first_msg.data:
            raw = first_msg.data[0]["user_input"] or ""
            if not raw.startswith("(system)"):
                title = raw[:60] + ("…" if len(raw) > 60 else "")
        count_res = (
            supabase.table("conversations")
            .select("id", count="exact")
            .eq("session_id", sid)
            .execute()
        )
        msg_count = getattr(count_res, "count", None) or len(count_res.data or [])
        sessions.append({
            "id": sid,
            "title": title or "Session",
            "mode": row.get("current_mode", "learn"),
            "current_mode": row.get("current_mode", "learn"),
            "prefers_video": row.get("prefers_video", False),
            "course_id": row.get("course_id"),
            "message_count": msg_count,
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
        })
    return {"sessions": sessions, "total": len(sessions), "limit": limit, "offset": offset}


@router.get("/courses")
def list_courses(user=Depends(auth_guard)):
    res = get_user_client(user["token"]).table("courses").select("id, name, description").execute()
    return {"courses": res.data or []}


@router.post("/")
def create_session(payload: SessionCreateRequest, user=Depends(auth_guard)):
    supabase = get_user_client(user["token"])

    course = supabase.table("courses").select("id").eq("id", payload.course_id).execute()
    if not course.data:
        raise HTTPException(status_code=404, detail=f"Course '{payload.course_id}' not found")

    mode = (payload.mode or "learn").lower().strip()
    if mode not in ("learn", "application", "review"):
        raise HTTPException(status_code=400, detail="mode must be learn|application|review")

    ins = supabase.table("sessions").insert({
        "user_id": user["id"],
        "course_id": payload.course_id,
        "current_mode": mode,
        "prefers_video": payload.prefers_video,
        "started_at": _now_iso(),
    }).execute()

    if not ins.data:
        raise HTTPException(status_code=500, detail="Failed to create session")

    row = ins.data[0]
    return {
        "session_id": row["id"],
        "course_id": row["course_id"],
        "current_mode": row["current_mode"],
        "prefers_video": row["prefers_video"],
        "started_at": row.get("started_at"),
    }


@router.get("/{session_id}")
def get_session(session_id: str, user=Depends(auth_guard)):
    sid = _validate_uuid(session_id)
    res = (
        get_user_client(user["token"])
        .table("sessions")
        .select("id,current_mode,prefers_video,started_at,ended_at")
        .eq("id", sid)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Session not found")
    row = res.data[0]
    return {
        "session_id": sid,
        "current_mode": row["current_mode"],
        "prefers_video": row["prefers_video"],
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
    }


@router.patch("/{session_id}/mode")
def set_session_mode(session_id: str, payload: SessionModeUpdate, user=Depends(auth_guard)):
    sid = _validate_uuid(session_id)
    mode = (payload.current_mode or "").lower().strip()
    if mode not in ("learn", "application", "review"):
        raise HTTPException(status_code=400, detail="current_mode must be learn|application|review")

    supabase = get_user_client(user["token"])
    res = supabase.table("sessions").select("prefers_video").eq("id", sid).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Session not found")

    update_payload = {"current_mode": mode}
    if mode == "review":
        update_payload["prefers_video"] = False  # review mode is text-only

    supabase.table("sessions").update(update_payload).eq("id", sid).execute()

    prefers_video = update_payload.get("prefers_video", res.data[0].get("prefers_video", False))
    return {"session_id": sid, "current_mode": mode, "prefers_video": prefers_video}


@router.patch("/{session_id}/video")
def set_video_preference(session_id: str, payload: VideoToggle, user=Depends(auth_guard)):
    sid = _validate_uuid(session_id)
    supabase = get_user_client(user["token"])

    res = (
        supabase.table("sessions")
        .select("current_mode")
        .eq("id", sid)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Session not found")

    if res.data[0]["current_mode"] == "review" and payload.prefers_video:
        raise HTTPException(status_code=400, detail="Review mode is text-only. Video responses are disabled.")

    supabase.table("sessions").update({"prefers_video": payload.prefers_video}).eq("id", sid).execute()
    return {"session_id": sid, "prefers_video": payload.prefers_video}


@router.get("/{session_id}/conversations")
def get_conversations(
    session_id: str,
    limit: int = Query(50, ge=1, le=200),  # M-6: cap to prevent full-table dumps
    offset: int = Query(0, ge=0),
    user=Depends(auth_guard),
):
    sid = _validate_uuid(session_id)
    supabase = get_user_client(user["token"])

    # Verify the session exists and belongs to this user (RLS filters by ownership,
    # but we want a clean 404 rather than an empty conversations list on bad IDs).
    if not supabase.table("sessions").select("id").eq("id", sid).execute().data:
        raise HTTPException(status_code=404, detail="Session not found")

    convos = (
        supabase.table("conversations")
        .select("id,mode,user_input,tutor_response,response_format,video_url,audio_url,created_at")
        .eq("session_id", sid)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {
        "session_id": sid,
        "conversations": list(reversed(convos.data or [])),
        "limit": limit,
        "offset": offset,
    }


@router.patch("/{session_id}/title")
def rename_session(session_id: str, payload: SessionTitleUpdate, user=Depends(auth_guard)):
    sid = _validate_uuid(session_id)
    supabase = get_user_client(user["token"])
    if not supabase.table("sessions").select("id").eq("id", sid).execute().data:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        supabase.table("sessions").update({"title": payload.title}).eq("id", sid).execute()
    except Exception:
        pass  # graceful no-op if title column hasn't been added to the schema yet
    return {"session_id": sid, "title": payload.title}


@router.post("/{session_id}/end")
def end_session(session_id: str, user=Depends(auth_guard)):
    sid = _validate_uuid(session_id)
    supabase = get_user_client(user["token"])

    if not supabase.table("sessions").select("id").eq("id", sid).execute().data:
        raise HTTPException(status_code=404, detail="Session not found")

    now = _now_iso()
    supabase.table("sessions").update({"ended_at": now}).eq("id", sid).execute()
    return {"session_id": sid, "status": "ended", "ended_at": now}
