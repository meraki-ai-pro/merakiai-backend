from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core import assignments
from app.core.auth import auth_guard
from app.core.enrolment import (
    PARTICIPATING_STATUSES,
    list_enrolled_course_ids,
    require_enrolment,
    require_mode_enabled,
)
from app.db.supabase import get_supabase, get_user_client
from app.media.storage_service import STUDENT_UPLOADS_BUCKET, signed_url
from app.models.models import (
    SessionArchiveUpdate, SessionCreateRequest, SessionModeUpdate, SessionPinUpdate,
    SessionTitleUpdate, VideoToggle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["Sessions"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sign_attachments(attachments) -> list | None:
    """Swap stored storage paths for short-lived signed URLs.

    Only the path is persisted on the conversation row — student-uploads is a
    private bucket, so a raw path is useless to the browser and a permanent URL
    would expose one student's work to anyone who ever saw the link.
    """
    if not attachments:
        return None

    signed = []
    for item in attachments:
        path = item.get("path")
        if not path:
            continue
        url = signed_url(item.get("bucket", STUDENT_UPLOADS_BUCKET), path)
        if url:
            signed.append({"url": url, "media_type": item.get("media_type")})
    return signed or None


def _validate_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid session ID format")


def _delete_session_uploads(supabase, user_id: str, session_id: str) -> int:
    """Remove private images stored beneath ``user/session`` before DB rows.

    Conversation rows contain the only durable references to these objects.
    Removing storage first prevents a successful database delete from leaving
    photographs of a student's work orphaned in the bucket.
    """
    folder = f"{user_id}/{session_id}"
    entries = supabase.storage.from_(STUDENT_UPLOADS_BUCKET).list(folder) or []
    paths = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else getattr(entry, "name", None)
        if name:
            paths.append(f"{folder}/{name}")
    if paths:
        supabase.storage.from_(STUDENT_UPLOADS_BUCKET).remove(paths)
    return len(paths)


def _delete_session_data(supabase, session_id: str) -> None:
    """Delete one session's data in foreign-key-safe order.

    The production schema predates cascade deletes on session foreign keys, so
    the API must remove child rows explicitly. Analytics and feedback that are
    intentionally retained lose their session pointer instead of blocking the
    parent delete.
    """
    for table in ("user_feedback", "feedback_responses", "events"):
        supabase.table(table).update({"session_id": None}).eq(
            "session_id", session_id
        ).execute()

    for table in (
        "session_state",
        "mode_feedback",
        "review_attempts",
        "review_summaries",
        "session_surveys",
        "conversations",
        "mode_sessions",
        "request_metrics",
        "ws_sessions",
    ):
        supabase.table(table).delete().eq("session_id", session_id).execute()

    supabase.table("sessions").delete().eq("id", session_id).execute()


@router.get("/")
def list_sessions(
    user=Depends(auth_guard),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    archived: bool = Query(False, description="True lists only archived sessions"),
):
    """Return the user's sessions, newest first.

    Archived sessions are left out of the sidebar list and shown on their own
    page (``?archived=true``). Pinned sessions come back flagged; the client
    groups them at the top.
    """
    supabase = get_user_client(user["token"])

    def _rows(columns: str, *, filter_archived: bool):
        query = supabase.table("sessions").select(columns).eq("user_id", user["id"])
        if filter_archived:
            query = (
                query.not_.is_("archived_at", "null") if archived
                else query.is_("archived_at", "null")
            )
        return (
            query.order("started_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )

    base_columns = "id, current_mode, prefers_video, started_at, ended_at, course_id"
    try:
        rows = _rows(base_columns + ", title, pinned_at, archived_at", filter_archived=True)
    except Exception:
        # sql/018 not applied: nothing can be archived yet, so the archive is
        # empty and the main list is every session, as before.
        if archived:
            return {"sessions": [], "total": 0, "limit": limit, "offset": offset}
        try:
            rows = _rows(base_columns + ", title", filter_archived=False)
        except Exception:
            # sql/016 not applied either: no stored titles, every one is derived.
            rows = _rows(base_columns, filter_archived=False)
    session_rows = rows.data or []
    session_ids = [row["id"] for row in session_rows]
    titles: dict[str, str] = {}
    message_counts: dict[str, int] = {}

    # Fetch the sidebar metadata in one request. The previous implementation
    # issued two sequential queries per session (title + count), turning a
    # 50-session sidebar into 101 Supabase round trips.
    if session_ids:
        conversation_rows = (
            supabase.table("conversations")
            .select("session_id,user_input")
            .in_("session_id", session_ids)
            .order("created_at", desc=True)
            .range(0, 4999)
            .execute()
        )
        for conversation in conversation_rows.data or []:
            sid = conversation.get("session_id")
            if not sid:
                continue
            message_counts[sid] = message_counts.get(sid, 0) + 1
            raw = (conversation.get("user_input") or "").strip()
            if raw and not raw.startswith("(system)"):
                # Results are newest first. Reassigning leaves the oldest
                # available real user prompt as the stable session title.
                titles[sid] = raw

    sessions = []
    for row in session_rows:
        sid = row["id"]
        # A name the student chose wins over one derived from their first message.
        title = (row.get("title") or "").strip() or titles.get(sid, "")
        sessions.append({
            "id": sid,
            "title": (title[:60] + ("…" if len(title) > 60 else "")) if title else "Session",
            "mode": row.get("current_mode", "learn"),
            "current_mode": row.get("current_mode", "learn"),
            "prefers_video": row.get("prefers_video", False),
            "course_id": row.get("course_id"),
            "message_count": message_counts.get(sid, 0),
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "pinned_at": row.get("pinned_at"),
            "archived_at": row.get("archived_at"),
        })
    return {"sessions": sessions, "total": len(sessions), "limit": limit, "offset": offset}


@router.get("/courses")
def list_courses(user=Depends(auth_guard)):
    """Courses this student may open.

    Previously returned the whole catalogue, which let any student see every
    course on the platform and hand the id straight to POST /sessions/. The
    enrolment check now rejects that, but the list should not advertise it
    either.

    TRANSITIONAL: a caller with no enrolments at all still sees everything.
    Right now nobody has an enrolment row, so filtering strictly would cut off
    every existing account the moment this deploys. Remove this fallback (and
    return an empty list, prompting the user for an invite code) once the
    pilot cohort has been enrolled — see task #16.
    """
    supabase = get_user_client(user["token"])
    query = supabase.table("courses").select("id, name, description")

    course_ids = list_enrolled_course_ids(user["id"])
    if course_ids:
        query = query.in_("id", course_ids)

    res = query.execute()
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

    # Permission stack steps 3 and 4 — Student Permission Checks §3.1.
    # Starting a session is new work, so a completed enrolment is not enough.
    require_enrolment(user, payload.course_id, PARTICIPATING_STATUSES)
    require_mode_enabled(payload.course_id, mode)
    assignments.require_unlocked(user["id"], payload.course_id)

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


@router.get("/activities/{course_id}")
def list_activities(course_id: str, user=Depends(auth_guard)):
    """What the lecturer has assigned this student on the course (Intervention
    Studio), and whether an unfinished required one locks other study."""
    require_enrolment(user, course_id)
    items = assignments.for_student(user["id"], course_id)
    return {"activities": items, "locked": bool(assignments.blocking(user["id"], course_id))}


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


@router.delete("/{session_id}")
def delete_session(session_id: str, user=Depends(auth_guard)):
    """Permanently delete a session owned by the authenticated user."""
    sid = _validate_uuid(session_id)

    # Authorise with the caller's JWT and an explicit owner predicate. RLS is
    # the primary boundary; the user_id filter is defence in depth and keeps a
    # clean 404 response for both missing and somebody else's sessions.
    owned = (
        get_user_client(user["token"])
        .table("sessions")
        .select("id,user_id")
        .eq("id", sid)
        .eq("user_id", user["id"])
        .execute()
    )
    if not owned.data:
        raise HTTPException(status_code=404, detail="Session not found")

    # Deleting child rows needs the service role because those historical
    # tables do not all expose DELETE policies. Ownership was established
    # above before crossing that boundary.
    supabase = get_supabase()
    removed_uploads = _delete_session_uploads(supabase, user["id"], sid)
    _delete_session_data(supabase, sid)

    return {
        "session_id": sid,
        "status": "deleted",
        "storage_objects_deleted": removed_uploads,
    }


@router.patch("/{session_id}/mode")
def set_session_mode(session_id: str, payload: SessionModeUpdate, user=Depends(auth_guard)):
    sid = _validate_uuid(session_id)
    mode = (payload.current_mode or "").lower().strip()
    if mode not in ("learn", "application", "review"):
        raise HTTPException(status_code=400, detail="current_mode must be learn|application|review")

    supabase = get_user_client(user["token"])
    res = supabase.table("sessions").select("prefers_video, course_id").eq("id", sid).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Session not found")

    # Switching an existing session into Application mode is the same privilege
    # as starting one there — otherwise the check at creation is trivially
    # sidestepped by opening a Learn session and flipping the mode.
    course_id = res.data[0].get("course_id")
    if course_id:
        require_mode_enabled(course_id, mode)

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
    owned = supabase.table("sessions").select("id, course_id").eq("id", sid).execute().data
    if not owned:
        raise HTTPException(status_code=404, detail="Session not found")
    # Reopening an old session is locked while a required activity waits.
    assignments.require_unlocked(user["id"], owned[0]["course_id"], sid)

    columns = (
        "id,mode,user_input,tutor_response,response_format,"
        "video_url,audio_url,created_at,sources,attachments"
    )
    try:
        convos = (
            supabase.table("conversations")
            .select(columns)
            .eq("session_id", sid)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
    except Exception:
        # Columns absent until 007_add_conversation_sources_and_file_retention.sql
        # is applied. A transcript without citations beats a 500.
        convos = (
            supabase.table("conversations")
            .select(columns.replace(",sources,attachments", ""))
            .eq("session_id", sid)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )

    rows = list(reversed(convos.data or []))
    for row in rows:
        row["attachments"] = _sign_attachments(row.get("attachments"))

    return {
        "session_id": sid,
        "conversations": rows,
        "limit": limit,
        "offset": offset,
    }


@router.patch("/{session_id}/title")
def rename_session(session_id: str, payload: SessionTitleUpdate, user=Depends(auth_guard)):
    sid = _validate_uuid(session_id)
    supabase = get_user_client(user["token"])
    if not supabase.table("sessions").select("id").eq("id", sid).execute().data:
        raise HTTPException(status_code=404, detail="Session not found")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be blank")
    try:
        supabase.table("sessions").update({"title": title}).eq("id", sid).execute()
    except Exception as exc:
        # This used to pass silently, so renames "succeeded" and reverted on the
        # next refresh. Say so instead: the fix is applying sql/016.
        logger.warning("Session rename failed for %s: %s", sid, exc)
        raise HTTPException(
            status_code=503, detail="Renaming sessions is not available yet."
        ) from exc
    return {"session_id": sid, "title": title}


def _set_session_flags(session_id: str, user: dict, updates: dict) -> None:
    """Write pin/archive columns on the caller's own session.

    The user client means RLS decides ownership, exactly as for rename; a
    session that is not theirs reads as missing.
    """
    sid = _validate_uuid(session_id)
    supabase = get_user_client(user["token"])
    if not supabase.table("sessions").select("id").eq("id", sid).execute().data:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        supabase.table("sessions").update(updates).eq("id", sid).execute()
    except Exception as exc:
        logger.warning("Session flag update failed for %s: %s", sid, exc)
        raise HTTPException(
            status_code=503, detail="Pinning and archiving are not available yet."
        ) from exc


@router.patch("/{session_id}/pin")
def pin_session(session_id: str, payload: SessionPinUpdate, user=Depends(auth_guard)):
    pinned_at = _now_iso() if payload.pinned else None
    _set_session_flags(session_id, user, {"pinned_at": pinned_at})
    return {"session_id": session_id, "pinned_at": pinned_at}


@router.patch("/{session_id}/archive")
def archive_session(session_id: str, payload: SessionArchiveUpdate, user=Depends(auth_guard)):
    archived_at = _now_iso() if payload.archived else None
    # Archiving unpins: an archived session is out of the sidebar, so a pin
    # on it would be invisible and would resurface oddly on unarchive.
    updates = {"archived_at": archived_at}
    if payload.archived:
        updates["pinned_at"] = None
    _set_session_flags(session_id, user, updates)
    return {"session_id": session_id, "archived_at": archived_at}


@router.post("/{session_id}/end")
def end_session(session_id: str, user=Depends(auth_guard)):
    sid = _validate_uuid(session_id)
    supabase = get_user_client(user["token"])

    if not supabase.table("sessions").select("id").eq("id", sid).execute().data:
        raise HTTPException(status_code=404, detail="Session not found")

    now = _now_iso()
    supabase.table("sessions").update({"ended_at": now}).eq("id", sid).execute()
    return {"session_id": sid, "status": "ended", "ended_at": now}
