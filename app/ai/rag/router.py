# app/ai/rag/router.py
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from celery.result import AsyncResult

from app.core.auth import auth_guard
from app.core.rate_limit import rate_limit
from app.db.supabase import get_async_supabase
from app.media.stt_service import transcribe_audio
from app.models.models import RagTurnRequest
from app.ai.tasks import process_rag_turn_task
from app.core.celery_app import celery_app

router = APIRouter(prefix="/rag", tags=["RAG"])

# M-5: 25 MB cap on voice audio uploads
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


async def _set_session_mode(session_id: str, user_id: str, mode: str) -> None:
    """
    Keeps sessions.current_mode consistent with actual usage.
    Also enforces ownership.
    """
    supabase = await get_async_supabase()
    res = await (
        supabase.table("sessions")
        .select("id,user_id,current_mode")
        .eq("id", session_id)
        .execute()
    )
    if not res.data:
        return

    row = res.data[0]
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not allowed")

    if (row.get("current_mode") or "").lower().strip() != mode:
        await supabase.table("sessions").update({"current_mode": mode}).eq(
            "id", session_id
        ).execute()


async def _get_prefers_video(session_id: str) -> bool:
    supabase = await get_async_supabase()
    res = await supabase.table("sessions").select("prefers_video").eq("id", session_id).execute()
    return bool(res.data[0].get("prefers_video", False)) if res.data else False


@router.post("/turn")
async def rag_turn(
    payload: RagTurnRequest,
    user=Depends(auth_guard),
    _rl=Depends(rate_limit(max_calls=30, window_seconds=60)),
):
    # Enforce learn-only
    mode = (payload.mode or "learn").lower().strip()
    if mode != "learn":
        raise HTTPException(
            status_code=400,
            detail="Practice/Review are handled by /mode-sessions. Use mode='learn' here.",
        )

    # Validate session exists and belongs to user
    # Sessions must be pre-created via POST /sessions/ with a course_id
    supabase = await get_async_supabase()
    res = await supabase.table("sessions").select("id,user_id,course_id").eq("id", payload.session_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Session not found. Create one via POST /sessions/")
    row = res.data[0]
    if row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not allowed")
    if not row.get("course_id"):
        raise HTTPException(status_code=400, detail="Session has no course assigned. Re-create via POST /sessions/")

    await _set_session_mode(payload.session_id, user["id"], "learn")

    # Route to video_tasks (priority 3) or text_tasks (priority 9) based on session preference
    prefers_video = await _get_prefers_video(payload.session_id)
    queue    = "video_tasks" if prefers_video else "text_tasks"
    priority = 3             if prefers_video else 9

    # M-17: pass prefers_video to avoid a duplicate DB lookup inside the Celery task
    task = process_rag_turn_task.apply_async(
        args=[payload.session_id, user["id"], payload.message, "learn", prefers_video],
        queue=queue,
        priority=priority,
    )
    return {"status": "processing", "task_id": task.id}


@router.get("/status/{task_id}")
async def get_task_status(task_id: str, user=Depends(auth_guard)):
    """Poll for the result of a /rag/turn task."""
    res = AsyncResult(task_id, app=celery_app)
    if res.ready():
        if res.successful():
            return res.result
        else:
            return {"status": "failed", "error": str(res.result)}
    return {"status": "processing"}


@router.post("/turn/voice")
async def rag_turn_voice(
    session_id: str,
    file: UploadFile = File(...),
    user=Depends(auth_guard),
    _rl=Depends(rate_limit(max_calls=30, window_seconds=60)),
):
    """
    Voice input for Learn mode: STT -> same /rag/turn logic.
    """
    # M-5: enforce size limit before reading the full file into memory
    audio_bytes = await file.read(_MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file exceeds the 25 MB limit.",
        )
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload")

    transcript = await asyncio.to_thread(transcribe_audio, audio_bytes, file.filename)

    payload = RagTurnRequest(session_id=session_id, mode="learn", message=transcript)
    out = await rag_turn(payload, user)
    out["transcript"] = transcript
    return out
