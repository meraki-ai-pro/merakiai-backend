from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from celery.result import AsyncResult

from app.core.auth import auth_guard
from app.core.enrolment import (
    PARTICIPATING_STATUSES,
    require_enrolment,
    require_mode_enabled,
)
from app.core.rate_limit import rate_limit
from app.db.supabase import get_user_client
from app.media.stt_service import transcribe_audio
from app.models.models import ModeSessionStartRequest, ModeSessionTurnRequest
from app.ai.tasks import process_mode_session_start_task, process_mode_session_turn_task
from app.core.celery_app import celery_app

router = APIRouter(prefix="/mode-sessions", tags=["Mode Sessions"])

# M-5: 25 MB cap on voice audio uploads
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_chat_session_ownership(supabase, session_id: str, user_id: str) -> str:
    """Validate the session and return its course_id."""
    # Sessions must be pre-created with a course_id — never auto-create here.
    try:
        UUID(str(session_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid session_id format. Expected UUID.")

    s = supabase.table("sessions").select("course_id").eq("id", session_id).execute()
    if not s.data:
        raise HTTPException(status_code=404, detail="Session not found. Create one via POST /sessions/")
    course_id = s.data[0].get("course_id")
    if not course_id:
        raise HTTPException(status_code=400, detail="Session has no course assigned. Re-create via POST /sessions/")
    return course_id


def _ensure_mode_session_ownership(supabase, mode_session_id: str, user_id: str):
    # Avoid .single() — it raises PGRST116 when 0 rows are returned.
    ms = supabase.table("mode_sessions").select("*").eq("id", mode_session_id).execute()
    if not ms.data:
        raise HTTPException(
            status_code=404,
            detail=f"Mode session {mode_session_id} not found. Start with POST /mode-sessions/start.",
        )
    return ms.data[0]


async def _stt(audio_bytes: bytes, filename: str) -> str:
    out = transcribe_audio(audio_bytes, filename)
    if hasattr(out, "__await__"):
        out = await out
    transcript = (out or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="Could not transcribe audio")
    return transcript


@router.get("/")
def list_mode_sessions(
    session_id: str,
    limit: int = Query(20, ge=1, le=100),  # M-6: bounded pagination
    offset: int = Query(0, ge=0),
    user=Depends(auth_guard),
):
    supabase = get_user_client(user["token"])
    res = (
        supabase.table("mode_sessions")
        .select("id,mode,session_type,difficulty,total_items,current_item,completed,started_at,ended_at,message_count")
        .eq("session_id", session_id)
        .order("started_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {
        "session_id": session_id,
        "mode_sessions": res.data or [],
        "limit": limit,
        "offset": offset,
    }


@router.get("/status/{task_id}")
async def get_mode_session_task_status(task_id: str, user=Depends(auth_guard)):
    res = AsyncResult(task_id, app=celery_app)
    if res.ready():
        return res.result if res.successful() else {"status": "failed", "error": str(res.result)}
    return {"status": "processing"}


@router.post("/start")
async def start_mode_session(
    payload: ModeSessionStartRequest,
    user=Depends(auth_guard),
    _rl=Depends(rate_limit(max_calls=20, window_seconds=60)),
):
    supabase = get_user_client(user["token"])

    mode = payload.mode.lower().strip()
    stype = payload.session_type.lower().strip()
    difficulty = payload.difficulty.strip().title()

    if mode not in ("application", "review"):
        raise HTTPException(status_code=400, detail="mode must be application or review")

    course_id = _ensure_chat_session_ownership(supabase, payload.session_id, user["id"])

    # Permission stack steps 3 and 4. Application mode is opt-out per course,
    # so this is the gate the lecturer's practice_mode_enabled flag acts on.
    require_enrolment(user, course_id, PARTICIPATING_STATUSES)
    require_mode_enabled(course_id, mode)

    ms = (
        supabase.table("mode_sessions")
        .insert({
            "session_id": payload.session_id,
            "user_id": user["id"],
            "mode": mode,
            "session_type": stype,
            "difficulty": difficulty,
            "total_items": (payload.total_items or 10) if mode == "review" else 3,
            "current_item": 1,
            "completed": False,
            "started_at": _now_iso(),
        })
        .execute()
    )

    if not ms.data:
        raise HTTPException(status_code=500, detail="Failed to create mode session")

    mode_session_id = ms.data[0]["id"]

    task = process_mode_session_start_task.apply_async(
        args=[payload.session_id, user["id"], mode, stype, difficulty, mode_session_id],
        queue="text_tasks",
        priority=9,
    )

    return {
        "status": "processing",
        "task_id": task.id,
        "mode_session_id": mode_session_id,
    }


@router.post("/{mode_session_id}/turn")
async def mode_session_turn(
    mode_session_id: str,
    payload: ModeSessionTurnRequest,
    user=Depends(auth_guard),
    _rl=Depends(rate_limit(max_calls=30, window_seconds=60)),
):
    supabase = get_user_client(user["token"])

    ms = _ensure_mode_session_ownership(supabase, mode_session_id, user["id"])
    session_id = ms["session_id"]

    # Re-checked per turn so a withdrawal takes effect mid-session rather than
    # at next login. Uses ACTIVE_STATUSES, not PARTICIPATING: a student whose
    # enrolment completed part-way through a practice set should be allowed to
    # finish it rather than losing their progress at the last question.
    course_id = _ensure_chat_session_ownership(supabase, session_id, user["id"])
    require_enrolment(user, course_id)

    mode = ms["mode"]
    stype = ms["session_type"]
    difficulty = ms["difficulty"]
    total_items = int(ms.get("total_items") or 10)
    current_item = int(ms.get("current_item") or 1)

    st = supabase.table("session_state").select("*").eq("mode_session_id", mode_session_id).execute()
    if not st.data:
        raise HTTPException(status_code=409, detail="No active state for this mode session. Start again.")

    student_answer = payload.message.strip()
    if not student_answer:
        raise HTTPException(status_code=400, detail="message cannot be empty")

    state = st.data[0]
    task = process_mode_session_turn_task.apply_async(
        args=[
            mode_session_id, session_id, user["id"], mode, stype,
            student_answer, state["pending_payload"], state.get("contexts") or [],
            int(state.get("step", 1)), int(state.get("total_steps", 1)),
            difficulty, current_item, total_items,
        ],
        queue="text_tasks",
        priority=9,
    )

    return {"status": "processing", "task_id": task.id}


@router.post("/{mode_session_id}/end")
async def end_mode_session(mode_session_id: str, user=Depends(auth_guard)):
    import asyncio
    from app.core.analytics import close_mode_session

    supabase = get_user_client(user["token"])
    _ensure_mode_session_ownership(supabase, mode_session_id, user["id"])

    supabase.table("mode_sessions").update({
        "completed": True,
        "ended_at": _now_iso(),
    }).eq("id", mode_session_id).execute()

    supabase.table("session_state").delete().eq("mode_session_id", mode_session_id).execute()

    await asyncio.to_thread(close_mode_session, mode_session_id)

    return {"mode_session_id": mode_session_id, "status": "ended"}


@router.post("/{mode_session_id}/turn/voice")
async def mode_session_turn_voice(
    mode_session_id: str,
    file: UploadFile = File(...),
    user=Depends(auth_guard),
):
    # M-5: enforce size limit before reading the full file into memory
    audio_bytes = await file.read(_MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio file exceeds the 25 MB limit.")
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload")

    transcript = await _stt(audio_bytes, file.filename)
    payload = ModeSessionTurnRequest(message=transcript)
    out = await mode_session_turn(mode_session_id, payload, user)
    out["transcript"] = transcript
    return out
