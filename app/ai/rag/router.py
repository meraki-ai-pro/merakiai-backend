# app/ai/rag/router.py
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from celery.result import AsyncResult

from app.core.auth import auth_guard
from app.core.enrolment import require_enrolment
from app.core.rate_limit import rate_limit
from app.db.supabase import get_async_supabase
from app.media.image_input import (
    MAX_IMAGE_BYTES,
    ImageValidationError,
    validate_images,
)
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
    return await _dispatch_rag_turn(payload, user)


async def _dispatch_rag_turn(
    payload: RagTurnRequest,
    user: dict,
    images: list[dict] | None = None,
):
    """Shared body for the text, voice and image turn endpoints.

    Kept out of the route signature on purpose — an ``images`` parameter on the
    handler itself would be read by FastAPI as a request body field and change
    the public contract of POST /rag/turn.
    """
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

    # Re-checked every turn, not just at session creation. A lecturer who
    # withdraws a student expects that to bite immediately; checking only at
    # creation would let an already-open session run indefinitely.
    await asyncio.to_thread(require_enrolment, user, row["course_id"])

    await _set_session_mode(payload.session_id, user["id"], "learn")

    # Route to video_tasks (priority 3) or text_tasks (priority 9) based on session preference
    prefers_video = await _get_prefers_video(payload.session_id)
    queue    = "video_tasks" if prefers_video else "text_tasks"
    priority = 3             if prefers_video else 9

    # M-17: pass prefers_video to avoid a duplicate DB lookup inside the Celery task
    task = process_rag_turn_task.apply_async(
        args=[payload.session_id, user["id"], payload.message, "learn", prefers_video, images],
        queue=queue,
        priority=priority,
    )
    return {"status": "processing", "task_id": task.id}


@router.post("/turn/image")
async def rag_turn_image(
    session_id: str,
    message: str = Form(""),
    files: list[UploadFile] = File(...),
    user=Depends(auth_guard),
    _rl=Depends(rate_limit(max_calls=15, window_seconds=60)),
):
    """Learn-mode turn with photographs of the student's work attached.

    Rate limited harder than the text turn: each image costs roughly 1.6k input
    tokens and phone cameras produce them in bursts.
    """
    raw: list[tuple[bytes, str]] = []
    for upload in files:
        # Read one byte past the cap so an oversized file is rejected on its
        # size rather than after being pulled fully into memory.
        data = await upload.read(MAX_IMAGE_BYTES + 1)
        raw.append((data, upload.filename or "image"))

    try:
        images = validate_images(raw)
    except ImageValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not images:
        raise HTTPException(status_code=400, detail="No image was attached.")

    # Retrieval runs on the text alone, so a photo sent with no question would
    # otherwise search on an empty string and ground the answer in nothing.
    question = message.strip() or (
        "Check the attached work. Identify any mistakes, explain why they are "
        "wrong, and show the correct steps."
    )

    payload = RagTurnRequest(session_id=session_id, mode="learn", message=question)
    out = await _dispatch_rag_turn(payload, user, images=images)
    out["image_count"] = len(images)
    return out


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


@router.post("/transcribe")
async def transcribe_voice_input(
    file: UploadFile = File(...),
    user=Depends(auth_guard),
    _rl=Depends(rate_limit(max_calls=30, window_seconds=60)),
):
    """Transcribe voice input without dispatching a chat turn.

    The frontend submits the returned text through the normal WebSocket path,
    ensuring Learn, Practice, and Review share the same message lifecycle.
    """
    audio_bytes = await file.read(_MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio file exceeds the 25 MB limit.")
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload")

    transcript = await asyncio.to_thread(transcribe_audio, audio_bytes, file.filename)
    if not transcript.strip():
        raise HTTPException(status_code=400, detail="No speech could be detected in the recording.")
    return {"transcript": transcript.strip()}


@router.post("/turn/voice")
async def rag_turn_voice(
    session_id: str = Form(...),
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
    out = await _dispatch_rag_turn(payload, user)
    out["transcript"] = transcript
    return out
